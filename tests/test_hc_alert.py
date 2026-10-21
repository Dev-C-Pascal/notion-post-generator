"""Health Check, пункт 6: алерт — коли тривожити (поріг і значущість), як не дублювати, як доставляти й що робити зі збоєм
самого Health Check."""
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
import httpx  # noqa: E402
import httpx2  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_hc_compare import _judge  # noqa: E402  (той самий модуль, що вже завантажив pytest)

from app import hc_alert, hc_eval, hc_flow, hc_report, healthcheck  # noqa: E402
from app import hc_compare as cmp  # noqa: E402
from app.hc_compare import Row  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Draft  # noqa: E402

KEY = "claude-haiku-5-5/medium:test"
RUN = {"run_id": "hc-2", "model_version": "runpod-new:draft_grounded"}


def rows(fb_bad: int = 0, x_bad: int = 0, n: int = 20, *, style: int = 4, errors: int = 0, judged: bool = True,
         shift: int = 0, no_draft_fb: int = 0) -> list[Row]:
    """2n статей (n fb + n x): fb_bad / x_bad статей мають фейк (з номера shift), перші `errors` x-статей — збій моделі,
    перші `no_draft_fb` fb-статей — відмова моделі."""
    out = []
    for ch, bad in (("fb", fb_bad), ("x", x_bad)):
        for i in range(n):
            if ch == "x" and i < errors:
                out.append(Row(f"{ch}{i}", ch, draft="error"))
                continue
            if ch == "fb" and i < no_draft_fb:
                out.append(Row(f"{ch}{i}", ch, draft="refused"))
                continue
            fake = shift <= i < shift + bad
            out.append(Row(f"{ch}{i}", ch, title=f"T {ch}{i}", url=f"https://x/{ch}{i}", draft="ok",
                           eval="EVALUATED" if judged else None, fact_safety="not ok" if fake else "ok" if judged else None,
                           tier_a=int(fake) if judged else None, style=style if judged else None))
    return out


def compare(new: list[Row], base: list[Row] | None = None, base_run: str = "hc-1") -> cmp.Comparison:
    return cmp.compare(base_run, base or rows(), "hc-2", new)


def verdict(new: list[Row], base: list[Row] | None = None) -> hc_alert.Assessment:
    c = compare(new, base)
    return hc_alert.assess(c, None, c.new)


# ---------- коли тривожити ----------

def test_first_run_with_a_healthy_summary_is_just_info():
    a = hc_alert.assess(None, None, cmp.summarize_scopes(rows()))
    assert (a.level, a.findings, a.reasons) == ("info", [], [])


def test_nothing_changed_is_ok():
    assert verdict(rows()).level == "ok"


def test_big_and_significant_drop_is_critical_and_names_the_numbers():
    a = verdict(rows(6, 6))  # 12 з 40 статей втратили Fact ok: −30 п. п., p ≈ 0,0005
    assert a.level == "critical"
    assert a.reasons == [
        "Fact safety = ok: 100% → 70% (−30 п. п., p = <0,01) відносно попереднього прогону hc-1",
        "Fact safety = ok, FB: 100% → 70% (−30 п. п., p = 0,03) відносно попереднього прогону hc-1",
        "Fact safety = ok, X: 100% → 70% (−30 п. п., p = 0,03) відносно попереднього прогону hc-1"]


def test_moderate_significant_drop_is_a_warning_and_channels_need_more():
    a = verdict(rows(3, 2))  # 5 з 40: −12,5 п. п., p = 0,06; у каналах 3/20 при p = 0,25 і 2/20 — замало
    assert a.level == "warning"
    assert a.reasons == ["Fact safety = ok: 100% → 88% (−12 п. п., p = 0,06) відносно попереднього прогону hc-1"]


def test_noise_does_not_alert():
    assert verdict(rows(2, 1)).level == "ok"  # 3 з 40 = 7,5 п. п. — нижче порога
    big_but_random = verdict(rows(2, 1, n=10), rows(0, 0, n=10))  # 3 з 20 = 15 п. п., але p = 0,25: могло бути випадково
    assert big_but_random.level == "ok"
    swapped = compare(rows(4, 4, shift=4), rows(4, 4))  # 8 статей зіпсувались, ІНШІ 8 виправились: у середньому нічого
    d = swapped.delta("fact_ok_rate")
    assert (d.worse, d.better, d.delta) == (8, 8, 0) and d.p == 1.0
    assert hc_alert.assess(swapped, None, swapped.new).level == "ok"


def test_few_pairs_are_not_enough_to_judge():
    a = verdict(rows(4, 4, n=4), rows(0, 0, n=4))  # 8 статей: −100 п. п. на кожному каналі, але пар менше за MIN_PAIRS
    assert a.level == "ok"


def test_style_drop_and_lost_drafts_alert_too():
    a = verdict(rows(style=2))  # усі 40 статей: Style 4 → 2
    assert a.level == "critical"
    assert any(r.startswith("Style 4–5: 100% → 0% (−100 п. п.") for r in a.reasons)
    assert any(r.startswith("Середній Style: 4,00 → 2,00 (−2,00") for r in a.reasons)
    lost = verdict(rows(no_draft_fb=20))  # на всі 20 fb-статей модель тепер відмовляється
    assert lost.level == "critical"
    assert any(r.startswith("Модель дала драфт: 100% → 50% (−50 п. п.") for r in lost.reasons)


def test_baseline_has_its_own_wording_and_both_comparisons_count():
    prev, base = compare(rows()), compare(rows(6, 6), base_run="hc-0")
    a = hc_alert.assess(prev, base, prev.new)
    assert a.level == "critical" and a.reasons[0].endswith("відносно еталонного прогону hc-0")


def test_thresholds_come_from_the_environment(monkeypatch):
    c = compare(rows(3, 2))
    monkeypatch.setenv("HEALTHCHECK_WARN_PP", "20")
    assert hc_alert.assess(c, None, c.new).level == "ok"  # поріг підняли — п'ять статей уже не привід
    monkeypatch.setenv("HEALTHCHECK_WARN_PP", "8")
    monkeypatch.setenv("HEALTHCHECK_ALPHA_WARN", "0.01")
    assert hc_alert.assess(c, None, c.new).level == "ok"  # зажадали більшої впевненості
    monkeypatch.setenv("HEALTHCHECK_ALPHA_WARN", "0.2")
    monkeypatch.setenv("HEALTHCHECK_CRIT_PP", "10")
    monkeypatch.setenv("HEALTHCHECK_ALPHA_CRIT", "0.1")
    assert hc_alert.assess(c, None, c.new).level == "critical"  # знизили поріг critical до 10 п. п.


def test_model_failures_and_incomplete_evaluation_alert_without_any_baseline():
    summary = cmp.summarize_scopes(rows(errors=3, n=20))  # 3 з 40 = 7,5% збоїв — ще нормально
    assert hc_alert.assess(None, None, summary).level == "info"
    summary = cmp.summarize_scopes(rows(errors=4, n=20))  # 10%
    a = hc_alert.assess(None, None, summary)
    assert (a.level, a.reasons) == ("warning", ["Модель не відповіла на 4 з 40 статей (збій чи таймаут RunPod)"])
    summary = cmp.summarize_scopes(rows(errors=10, n=20))  # 25%
    assert hc_alert.assess(None, None, summary).level == "critical"
    nothing = cmp.summarize_scopes([Row("a", "fb", draft="error"), Row("b", "x", draft="refused")])
    assert hc_alert.assess(None, None, nothing).reasons == ["Модель не дала жодного драфта на 2 статей"]
    unjudged = cmp.summarize_scopes(rows(judged=False))
    a = hc_alert.assess(None, None, unjudged)
    assert a.level == "warning" and a.reasons == ["Суддя оцінив лише 0% драфтів (0 з 40): цифрам звіту не варто вірити"]


def test_reasons_put_critical_first():
    c = compare(rows(6, 6))
    a = hc_alert.assess(c, None, cmp.summarize_scopes(rows(errors=4)))  # збої моделі (warning) + провал Fact safety (critical)
    assert [f.level for f in a.findings][0] == "warning"  # знайдено першим
    assert a.reasons[0].startswith("Fact safety") and a.reasons[-1].startswith("Модель не відповіла")  # показано останнім


# ---------- повідомлення й доставка ----------

def test_message_is_short_and_points_to_the_report():
    a = verdict(rows(6, 6))
    text = hc_alert.message(a, RUN, "https://notion.so/r")
    assert text.splitlines()[0] == "🔴 Health Check hc-2: погіршення"
    assert text.splitlines()[1].startswith("• Fact safety = ok: 100% → 70%")
    assert text.splitlines()[-2:] == ["Модель: runpod-new:draft_grounded", "Звіт: https://notion.so/r"]
    assert hc_alert.message(a, RUN, None).endswith("Звіт: GET /health-check/runs/hc-2/report (з сервера)")
    many = hc_alert.Assessment("warning", [hc_alert.Finding("warning", f"причина {i}") for i in range(11)])
    assert "• … і ще 3" in hc_alert.message(many, RUN, None) and "причина 8" not in hc_alert.message(many, RUN, None)


@pytest.fixture
def outbox(monkeypatch):
    """Усі вихідні HTTP-запити алерту йдуть у список; status — що відповідає «сервер»."""
    sent = {"requests": [], "status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["requests"].append((str(request.url), json.loads(request.content)))
        return httpx.Response(sent["status"])

    real = httpx.AsyncClient
    monkeypatch.setattr(hc_alert.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(hc_alert, "_recent", {})
    return sent


def test_channels_follow_the_environment(monkeypatch):
    for name in ("HEALTHCHECK_ALERT_WEBHOOK", "HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", "HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert hc_alert.channels() == []
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc")
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", "123:secret")
    assert hc_alert.channels() == ["webhook"]  # Telegram без chat_id не вважається налаштованим
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID", "-100")
    assert hc_alert.channels() == ["webhook", "telegram"]


async def test_send_reaches_every_channel_and_one_failure_does_not_stop_the_other(monkeypatch, outbox, caplog):
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc")
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID", "-100")
    assert await hc_alert.send("привіт", level="critical") == {"webhook": True, "telegram": True}
    (url1, body1), (url2, body2) = outbox["requests"]
    assert url1 == "https://hooks.example/abc" and body1 == {"text": "привіт", "content": "привіт", "level": "critical"}
    assert url2 == "https://api.telegram.org/bot123:secret/sendMessage"
    assert body2 == {"chat_id": "-100", "text": "привіт", "disable_web_page_preview": True}
    outbox.update(status=500, requests=[])
    with caplog.at_level(logging.WARNING):
        assert await hc_alert.send("ще раз") == {"webhook": False, "telegram": False}
    assert len(outbox["requests"]) == 2  # обидва канали спробувано
    assert "webhook" in caplog.text and "HTTPStatusError" in caplog.text
    assert "hooks.example" not in caplog.text and "123:secret" not in caplog.text  # адреса й токен у журнал не потрапляють


async def test_deliver_notifies_once_per_level_and_remembers_only_what_arrived(seed_run, monkeypatch, outbox):
    seed_run("hc-2")
    hc_report.save_report("hc-2", KEY, markdown="# r\n", summary={}, base_run="hc-1", baseline_run=None, level="warning",
                          reasons=[])
    warning = hc_alert.Assessment("warning", [hc_alert.Finding("warning", "Fact safety просів")])
    critical = hc_alert.Assessment("critical", [hc_alert.Finding("critical", "Fact safety обвалився")])
    ok = hc_alert.Assessment("ok")
    monkeypatch.delenv("HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc")

    assert await hc_alert.deliver("hc-2", KEY, ok, RUN, None) is None and outbox["requests"] == []  # ok — мовчимо
    outbox["status"] = 503  # канал недоступний — стан не записуємо, щоб спробувати ще раз
    assert await hc_alert.deliver("hc-2", KEY, warning, RUN, None) == {"webhook": False}
    assert hc_alert.sent_level("hc-2", KEY) is None
    outbox["status"] = 200
    assert await hc_alert.deliver("hc-2", KEY, warning, RUN, "https://notion.so/r") == {"webhook": True}
    assert hc_alert.sent_level("hc-2", KEY) == "warning"
    assert await hc_alert.deliver("hc-2", KEY, warning, RUN, None) is None  # про цей рівень уже сповістили
    assert await hc_alert.deliver("hc-2", KEY, critical, RUN, None) == {"webhook": True}  # стало гірше — ще раз
    assert hc_alert.sent_level("hc-2", KEY) == "critical"
    assert await hc_alert.deliver("hc-2", KEY, warning, RUN, None) is None
    assert await hc_alert.deliver("hc-2", KEY, critical, RUN, None, force=True) == {"webhook": True}
    texts = [body["text"] for _, body in outbox["requests"] if body.get("level") in ("warning", "critical")]
    assert texts[1].startswith("⚠️ Health Check hc-2: є ознаки погіршення") and texts[1].endswith("Звіт: https://notion.so/r")
    monkeypatch.setenv("HEALTHCHECK_ALERT_MIN_LEVEL", "critical")  # попередження вимкнено
    assert await hc_alert.deliver("hc-2", KEY, warning, RUN, None, force=True) is None


async def test_deliver_without_channels_only_logs(seed_run, monkeypatch, outbox, caplog):
    seed_run("hc-2")
    hc_report.save_report("hc-2", KEY, markdown="# r\n", summary={}, base_run=None, baseline_run=None, level="critical",
                          reasons=[])
    for name in ("HEALTHCHECK_ALERT_WEBHOOK", "HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with caplog.at_level(logging.WARNING):
        sent = await hc_alert.deliver("hc-2", KEY, hc_alert.Assessment("critical", [hc_alert.Finding("critical", "біда")]),
                                      RUN, None)
    assert sent == {} and "біда" in caplog.text and hc_alert.sent_level("hc-2", KEY) is None and outbox["requests"] == []


async def test_a_failure_of_health_check_itself_is_reported_but_not_repeated(monkeypatch, outbox):
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc")
    first = await hc_alert.notify_problem("Health Check hc-2 не завершився", "RuntimeError: RunPod 500")
    assert first == {"webhook": True}
    assert outbox["requests"][0][1]["text"] == "🔴 Health Check hc-2 не завершився\nRuntimeError: RunPod 500"
    again = await hc_alert.notify_problem("Health Check hc-2 не завершився", "RuntimeError: RunPod 500")
    assert again == {}  # те саме повідомлення — не частіше, ніж раз на PROBLEM_REPEAT_S
    assert await hc_alert.notify_problem("Health Check hc-3 не завершився") == {"webhook": True}  # інше — одразу
    monkeypatch.setattr(hc_alert, "PROBLEM_REPEAT_S", 0)
    assert await hc_alert.notify_problem("Health Check hc-2 не завершився", "RuntimeError: RunPod 500") == {"webhook": True}


# ---------- ланцюжок ----------

async def test_a_regression_is_reported_once_and_the_page_title_carries_the_level(seed_run, monkeypatch, outbox):
    seed_run("hc-1", started_at="2026-10-01T10:00:00Z")
    seed_run("hc-2", started_at="2026-10-08T10:00:00Z")
    _judge("hc-1", {"a1": ("ok", 4), "a2": ("ok", 4)})
    _judge("hc-2", {"a1": ("not ok", 2), "a2": ("not ok", 2)})
    # на двох статтях значущості не бути: відкриваємо p-захист і MIN_PAIRS, щоб перевірити саму проводку
    monkeypatch.setattr(hc_alert, "MIN_PAIRS", 1)
    monkeypatch.setenv("HEALTHCHECK_ALPHA_WARN", "1")
    monkeypatch.setenv("HEALTHCHECK_ALPHA_CRIT", "1")
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc")
    titles = []

    async def fake_create(parent, title, blocks):
        titles.append(title)
        return {"url": "https://www.notion.so/r"}

    monkeypatch.setenv("NOTION_HEALTHCHECK_PAGE_ID", "p" * 32)
    monkeypatch.setattr(hc_report.notion, "create_page", fake_create)
    md = await hc_flow.make_report("hc-2", save=True)
    assert md and md.startswith("# Health Check · hc-2 · 2026-10-08\n\n> 🔴 **Погіршення**\n\n")
    # 1 з 4 статей без відповіді моделі — теж критично (25%), і воно першим; далі — те, що перевірили
    assert "- Модель не відповіла на 1 з 4 статей (збій чи таймаут RunPod)\n" in md
    assert "- Fact safety = ok: 100% → 0% (−100 п. п., p = 0,50) відносно попереднього прогону hc-1\n" in md
    stored = hc_report.get_report("hc-2")
    assert stored and stored["level"] == "critical" and stored["alert_level"] == "critical" and stored["alert_at"]
    assert titles == ["🔴 Health Check · hc-2 · 2026-10-08"]
    assert len(outbox["requests"]) == 1 and outbox["requests"][0][1]["report_url"] == "https://www.notion.so/r"
    await hc_flow.make_report("hc-2", save=True)  # звіт перебудовано — друге сповіщення не йде
    assert len(outbox["requests"]) == 1
    await hc_flow.make_report("hc-2")  # без save нічого не надсилається й не зберігається
    assert len(outbox["requests"]) == 1


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
                          level="info", reasons=[])
    assert not hc_flow.followup_pending("hc-2")
    await hc_flow.resume_followups()
    assert ran == ["hc-2"]


async def test_followups_do_not_pay_again_for_drafts_the_judge_already_failed_on(seed_run):
    seed_run("hc-1", started_at="2026-10-01T10:00:00Z")
    key = hc_eval.current_judge_key()
    for case in hc_eval.pending_cases("hc-1", key):  # суддя збоїв на обох драфтах
        hc_eval.save_eval(hc_eval._row(case, key, outcome="ERROR", error="ParseError: немає блоку RESULT"))
    hc_report.save_report("hc-1", key, markdown="# r\n", summary={}, base_run=None, baseline_run=None, level="warning",
                          reasons=[])
    assert not hc_flow.followup_pending("hc-1")  # звіт є, спроби були — щогодини той самий платний цикл не запускаємо
    assert cmp.pending_evals("hc-1", key) == 2  # а для порівняння вони й надалі недооцінені


def test_alert_test_endpoint(monkeypatch):
    async def fake_send(text, **extra):
        return {"webhook": True}

    monkeypatch.setattr(hc_alert, "send", fake_send)
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc")
    with TestClient(app) as c:
        assert c.post("/health-check/alert-test").status_code == 401
        r = c.post("/health-check/alert-test", headers={"x-webhook-secret": "t"})
    assert r.json() == {"channels": ["webhook"], "sent": {"webhook": True}}


# ---------- наскрізно: два прогони моделі → оцінка → порівняння → звіт → алерт ----------

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


async def test_two_runs_end_to_end_a_worse_model_raises_one_alert(seed_run, outbox, monkeypatch):
    """Справжні: Postgres, SDK Anthropic (підроблено лише мережу), весь код Health Check. Підроблені: модель драфтів,
    відповіді судді, Notion і вебхук."""
    seed_run("hc-0", status="failed")  # лише щоб засіяти 4 еталонні статті; сам прогін не завершений і базою не стає
    for name, value in (("ANTHROPIC_API_KEY", "sk-test"), ("HEALTHCHECK_EVAL", "1"), ("NOTION_HEALTHCHECK_PAGE_ID", "p" * 32),
                        ("HEALTHCHECK_ALERT_WEBHOOK", "https://hooks.example/abc"), ("HEALTHCHECK_ALPHA_WARN", "1"),
                        ("HEALTHCHECK_ALPHA_CRIT", "1")):  # на 4 статтях значущості не бути — перевіряємо проводку
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(hc_alert, "MIN_PAIRS", 1)
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
    pages = []

    async def fake_create_page(parent, title, blocks):
        pages.append((parent, title, len(blocks)))
        return {"url": f"https://www.notion.so/page-{len(pages)}"}

    monkeypatch.setattr(hc_report.notion, "create_page", fake_create_page)

    good = {"a1": "Нормальний пост 1", "a2": "Нормальний пост 2", "x1": "Normal thread 1", "x2": "Normal thread 2"}
    first_model = ScriptedModel("runpod-ep:v1", good)
    first_id, _ = healthcheck.start("manual", first_model)
    await healthcheck.run(first_id, first_model)

    first = hc_report.get_report(first_id)
    assert first and first["level"] == "info" and first["base_run"] is None  # перший прогін — це база
    assert len(api_calls) == 4 and outbox["requests"] == []  # чотири оцінки, жодного алерту
    assert len(pages) == 1 and pages[0][1].startswith("ℹ️ Health Check · " + first_id)

    worse = dict(good, a2="ПОГАНИЙ пост 2", x1="ПОГАНИЙ thread 1")  # нова версія моделі зіпсувала дві статті з чотирьох
    second_model = ScriptedModel("runpod-ep:v2", worse)
    second_id, _ = healthcheck.start("manual", second_model)
    await healthcheck.run(second_id, second_model)

    assert len(api_calls) == 6  # дві незмінені статті взято з минулого прогону — за них не платимо вдруге
    assert sorted(c.split("/")[-1] for c in api_calls[4:]) == ["a2", "x1"]
    reused = {e["article_id"]: e["reused_from"] for e in hc_eval.list_evals(second_id)}
    assert reused == {"a1": first_id, "a2": None, "x1": None, "x2": first_id}
    report = hc_report.get_report(second_id)
    assert report and report["level"] == "critical" and report["base_run"] == first_id and report["alert_level"] == "critical"
    md = report["markdown"]
    assert "(у базі було `runpod-ep:v1`: модель змінилась)" in md
    assert "| Fact safety = ok | 50% | 100% | −50 п. п. ▼ |" in md and "### Погіршилось (2)" in md
    assert "Title a2" in md and "Title x1" in md and "Title a1" not in md.split("### Погіршилось")[1]
    assert report["notion_url"] == "https://www.notion.so/page-2" and pages[1][1].startswith("🔴 Health Check · " + second_id)
    (url, body), = outbox["requests"]  # рівно один алерт на весь час
    assert url == "https://hooks.example/abc" and body["level"] == "critical" and body["run_id"] == second_id
    assert body["text"].startswith(f"🔴 Health Check {second_id}: погіршення\n• Fact safety = ok: 100% → 50% (−50 п. п., p = ")
    assert body["text"].endswith("Звіт: https://www.notion.so/page-2")
    assert "sk-test" not in body["text"]

    # ті самі дані вдруге: нічого не оцінюється заново, сторінка в Notion і алерт не дублюються
    await hc_flow.after_run(second_id)
    assert len(api_calls) == 6 and len(pages) == 2 and len(outbox["requests"]) == 1

    with TestClient(app) as c:  # і все це видно через API
        assert c.get(f"/health-check/runs/{second_id}/report").text == md
        compare = c.get(f"/health-check/compare?run={second_id}").json()
        evals = c.get(f"/health-check/runs/{second_id}/evals").json()
    fact = next(d for d in compare["deltas"]["all"] if d["key"] == "fact_ok_rate")
    assert (compare["base_run"], fact["base"], fact["new"], fact["worse"], fact["better"]) == (first_id, 1.0, 0.5, 2, 0)
    assert [(e["article_id"], e["fact_safety"], e["style"]) for e in evals] == [
        ("a1", "ok", 4), ("a2", "not ok", 2), ("x1", "not ok", 2), ("x2", "ok", 4)]
