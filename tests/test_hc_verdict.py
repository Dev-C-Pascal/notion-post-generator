"""Health Check, пункт 5: вердикт «пройшла / не пройшла» ставить код, а не агент. Поріг, шум моделі, перший прогін,
замало оцінок, збої моделі, спостереження, і база — лише останній УСПІШНИЙ прогін."""
import os

os.environ.setdefault("NOTION_TOKEN", "secret_xxx")
os.environ.setdefault("WEBHOOK_SECRET", "t")
os.environ.setdefault("NOTION_DATABASE_ID", "0" * 32)
os.environ.setdefault("RUNPOD_ENDPOINT_ID", "test")
os.environ.setdefault("RUNPOD_API_KEY", "test")
os.environ.setdefault("COMMS_DATABASE_URL", "")

import pytest  # noqa: E402
from test_hc_compare import _judge  # noqa: E402  (той самий модуль, що вже завантажив pytest)

from app import hc_compare as cmp  # noqa: E402
from app import hc_flow, hc_report, hc_verdict  # noqa: E402
from app.hc_compare import Row  # noqa: E402
from app.hc_verdict import BASELINE, FAILED, PASSED, UNKNOWN  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("HEALTHCHECK_THRESHOLD_PP", "HEALTHCHECK_NOISE_PP", "HEALTHCHECK_WARN_PP", "HEALTHCHECK_ALPHA_WARN"):
        monkeypatch.delenv(name, raising=False)


def rows(publishable: int, n: int = 100, *, errors: int = 0, unjudged: int = 0, refused: int = 0) -> list[Row]:
    """n статей, навпіл fb і x: перші `publishable` — fact ok і Style 4, решта — fact not ok і Style 2; останні `errors` —
    збій моделі, перед ними `refused` — відмова, а `unjudged` драфтів суддя ще не оцінив."""
    out = []
    for i in range(n):
        ch = "fb" if i % 2 == 0 else "x"
        if i >= n - errors:
            out.append(Row(f"a{i}", ch, draft="error"))
        elif i >= n - errors - refused:
            out.append(Row(f"a{i}", ch, draft="refused"))
        elif i >= n - errors - refused - unjudged:
            out.append(Row(f"a{i}", ch, draft="ok"))
        else:
            good = i < publishable
            out.append(Row(f"a{i}", ch, title=f"T{i}", url=f"https://x/{i}", draft="ok", eval="EVALUATED",
                           fact_safety="ok" if good else "not ok", tier_a=0 if good else 1, style=4 if good else 2))
    return out


def decide(new: list[Row], base: list[Row] | None = None, baseline: list[Row] | None = None) -> hc_verdict.Verdict:
    b = cmp.compare("hc-1", base, "hc-2", new) if base is not None else None
    e = cmp.compare("hc-0", baseline, "hc-2", new) if baseline is not None else None
    return hc_verdict.decide((b or e).new if (b or e) else cmp.summarize_scopes(new), b, e, run_id="hc-2")


# ---------- поріг ----------

def test_a_drop_of_exactly_the_threshold_still_passes():
    v = decide(rows(45), rows(50))  # 50% → 45%: рівно 5 п. п.
    assert (v.status, v.value, v.base_value, v.base_run, v.threshold_pp) == (PASSED, 0.45, 0.5, "hc-1", 5.0)
    assert v.delta_pp == pytest.approx(-5)
    assert v.reason == ("Можна публікувати: 45% проти 50% в останньому успішному прогоні hc-1 (−5 п. п.) — падіння в межах "
                        "порога 5 п. п.")


def test_more_than_the_threshold_fails():
    v = decide(rows(44), rows(50))  # −6 п. п.
    assert v.status == FAILED and v.reason.endswith("(−6 п. п.) — впало більше за поріг 5 п. п.")


def test_no_change_or_growth_passes():
    assert decide(rows(50), rows(50)).reason.endswith("(без змін) — не впало")
    up = decide(rows(60), rows(50))
    assert up.status == PASSED and up.reason.endswith("(+10 п. п.) — не впало")


def test_model_noise_raises_the_threshold(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_NOISE_PP", "8")  # два прогони тієї самої моделі розійшлись на 8 п. п.
    v = decide(rows(43), rows(50))  # −7: більше за 5, але в межах шуму
    assert (v.status, v.threshold_pp, v.noise_pp) == (PASSED, 8.0, 8.0)
    assert decide(rows(41), rows(50)).status == FAILED  # −9
    monkeypatch.setenv("HEALTHCHECK_NOISE_PP", "3")  # шум менший за поріг — поріг не знижується
    assert hc_verdict.threshold_pp() == 5.0
    monkeypatch.setenv("HEALTHCHECK_THRESHOLD_PP", "10")
    assert hc_verdict.threshold_pp() == 10.0 and decide(rows(41), rows(50)).status == PASSED


def test_a_model_that_refuses_more_does_not_look_better():
    # на драфтах, що лишились, частка та сама (40 з 80 = 50%), але від усіх 100 статей — 40% проти 50%
    v = decide(rows(40, refused=20), rows(50))
    assert v.status == FAILED and v.value == 0.4


# ---------- коли вердикту нема або він інший ----------

def test_first_run_is_the_baseline():
    v = decide(rows(50))
    assert (v.status, v.base_run, v.delta_pp) == (BASELINE, None, None)
    assert v.reason == "Можна публікувати: 50%. Це перший успішний прогін — з ним порівнюватимемо наступні."


def test_too_few_judged_drafts_mean_unknown():
    v = decide(rows(30, unjudged=21), rows(50))  # оцінено 79 з 100 драфтів < 80%
    assert v.status == UNKNOWN and v.reason.startswith("Суддя оцінив лише 79% драфтів (79 з 100)")
    assert decide(rows(30, unjudged=20), rows(50)).status == FAILED  # 80% — уже досить
    base_unjudged = decide(rows(50), rows(30, unjudged=30))
    assert base_unjudged.status == UNKNOWN and "з яким порівнюємо, оцінено лише на 70%" in base_unjudged.reason


def test_model_failures_fail_the_run_from_a_quarter():
    v = decide(rows(50, errors=25), rows(50))
    assert v.status == FAILED and v.reason == ("Модель не відповіла на 25 з 100 статей (збій чи таймаут RunPod): це не "
                                               "модель, що пише пости")
    some = decide(rows(50, errors=10), rows(50))  # 10% — ще вердикт за метрикою, але із застереженням
    assert some.problems == ['Модель не відповіла на 10 з 100 статей (збій чи таймаут RunPod) (догенерувати: '
                             'POST /health-check {"resume": "hc-2"})']
    nothing = decide([Row("a", "fb", draft="error"), Row("b", "x", draft="refused")])
    assert nothing.status == FAILED and nothing.reason == "Модель не дала жодного драфта на 2 статей"


def test_observations_show_where_it_got_worse():
    base, new = rows(50), rows(50)
    for r in new[1:40:2]:  # x-канал: 20 статей втратили Style 4–5, але fact лишився ok — «можна публікувати» інше
        r.style = 2 if r.fact_safety == "ok" else r.style
    v = decide(new, base, baseline=base)
    assert v.status == FAILED and v.value == pytest.approx(0.3)
    assert any(o.startswith("Style 4–5, X:") and "відносно останнього успішного прогону hc-1" in o for o in v.observations)
    assert any("відносно еталонного прогону hc-0" in o for o in v.observations)
    assert all(not o.startswith("Можна публікувати (fact ok + Style 4–5):") for o in v.observations)  # це сам вердикт


# ---------- шум ----------

def test_noise_counts_articles_that_flip_between_two_runs_of_the_same_model():
    a, b = rows(50), rows(50)
    for r in b[:3]:  # три статті стали «ні»
        r.fact_safety = "not ok"
    for r in b[60:62]:  # дві — «так»
        r.fact_safety, r.style = "ok", 4
    n = hc_verdict.noise(a, b)
    assert (n["pairs"], n["flips"]) == (100, 5)
    assert n["metrics"]["publishable_rate"]["diff"] == pytest.approx(1)
    assert n["recommended_pp"] == 1
    assert "HEALTHCHECK_NOISE_PP=1 (поріг вердикту = max(5, 1) = 5 п. п.)" in hc_flow.noise_text("hc-a", "hc-b", "k", n)


# ---------- база: лише останній успішний прогін (справжній Postgres) ----------

async def test_a_failed_run_never_becomes_the_base(seed_run, monkeypatch):
    monkeypatch.setattr(hc_verdict, "ERROR_FAIL", 1.0)  # у засіяних даних 1 з 4 статей без відповіді моделі
    seed_run("hc-1", started_at="2026-10-01T10:00:00Z")
    seed_run("hc-2", started_at="2026-10-08T10:00:00Z")
    seed_run("hc-3", started_at="2026-10-15T10:00:00Z")
    seed_run("hc-4", started_at="2026-10-22T10:00:00Z")
    _judge("hc-1", {"a1": ("ok", 4), "a2": ("ok", 4)})  # 2 з 4 = 50%
    _judge("hc-2", {"a1": ("ok", 4), "a2": ("not ok", 2)})  # 25%: не пройшла
    _judge("hc-3", {"a1": ("ok", 4), "a2": ("ok", 4)})  # 50%
    _judge("hc-4", {"a1": ("ok", 4), "a2": ("ok", 4)})
    key = _judge("hc-1", {})
    assert cmp.last_successful_run("hc-2", key) is None  # вердиктів ще нема
    for rid in ("hc-1", "hc-2", "hc-3"):
        await hc_flow.make_report(rid, save=True)
    assert [hc_report.get_report(r)["verdict"] for r in ("hc-1", "hc-2", "hc-3")] == [BASELINE, FAILED, PASSED]
    assert hc_report.get_report("hc-3")["base_run"] == "hc-1"  # не hc-2: модель, що провалилась, не знижує планку
    assert cmp.last_successful_run("hc-4", key) == "hc-3"
    assert cmp.last_successful_run("hc-3", key) == "hc-1"
    assert cmp.last_successful_run("hc-1", key) is None
    with hc_report.pg._connect() as c:  # прогін, що лишився без вердикту (unknown), теж не база
        c.execute("UPDATE hc_reports SET verdict = 'unknown' WHERE run_id = 'hc-3'")
    assert cmp.last_successful_run("hc-4", key) == "hc-1"
