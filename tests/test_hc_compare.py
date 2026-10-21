"""Health Check, пункт 4: метрики прогону й порівняння двох прогонів. Чиста частина — на синтетичних рядках, вибір бази й
завантаження — на справжньому Postgres (TEST_DATABASE_URL), інакше ці тести пропускаються."""
import os

os.environ.setdefault("NOTION_TOKEN", "secret_xxx")
os.environ.setdefault("WEBHOOK_SECRET", "t")
os.environ.setdefault("NOTION_DATABASE_ID", "0" * 32)
os.environ.setdefault("RUNPOD_ENDPOINT_ID", "test")
os.environ.setdefault("RUNPOD_API_KEY", "test")
os.environ.setdefault("COMMS_DATABASE_URL", "")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import hc_compare as cmp  # noqa: E402
from app import hc_eval, hc_flow  # noqa: E402
from app.hc_compare import Row  # noqa: E402
from app.main import app  # noqa: E402


def R(aid, ch, *, draft="ok", fact=None, style=None, a=None, b=None, ft=(), status=None, flagged=None, cost=0.0):
    """Рядок прогону. fact задано — стаття оцінена (EVALUATED), інакше суддя її не бачив."""
    return Row(article_id=aid, channel=ch, title=f"Title {aid}", url=f"https://x/{aid}", draft=draft,
               eval="EVALUATED" if fact or style else None, fact_safety=fact, tier_a=a, tier_b=b, style=style,
               status=status, failure_types=list(ft), cost_usd=cost)


BASE = [R("f1", "fb", fact="ok", style=4, a=0, b=0, ft=["weak structure"]),
        R("f2", "fb", fact="not ok", style=2, a=2, b=1, ft=["factual distortion", "weak structure"]),
        R("f3", "fb", draft="needs_manual_review", fact="not ok", style=3, a=1, b=0, ft=["hallucinated fact"]),
        R("f4", "fb", draft="refused"),
        R("x1", "x", fact="ok", style=5, a=0, b=1),
        R("x2", "x", fact="not ok", style=1, a=1, b=0, ft=["weak structure"]),
        R("x3", "x", draft="needs_manual_review", fact="ok", style=3, a=0, b=0),
        R("x4", "x", draft="error")]
NEW = [R("f1", "fb", fact="not ok", style=2, a=1, b=0),  # з'явився фейк і Style −2
       R("f2", "fb", fact="ok", style=4, a=0, b=1),  # фейк зник і Style +2
       R("f3", "fb", draft="needs_manual_review", fact="not ok", style=3, a=1, b=0),
       R("f4", "fb", fact="ok", style=4, a=0, b=0),  # раніше була відмова, тепер драфт
       R("x1", "x", fact="ok", style=5, a=0, b=1),
       R("x2", "x", fact="not ok", style=1, a=1, b=0),
       R("x3", "x", draft="needs_manual_review", fact="ok", style=3, a=0, b=0),
       R("x4", "x", draft="error")]


# ---------- метрики ----------

def test_sign_test_matches_exact_binomial():
    assert cmp.sign_test_p(0, 0) is None  # нема розбіжностей — нема що порівнювати
    assert cmp.sign_test_p(5, 5) == 1.0
    assert cmp.sign_test_p(9, 1) == pytest.approx(22 / 1024)  # 2 · (C(10,0) + C(10,1)) / 2^10
    assert cmp.sign_test_p(1, 9) == cmp.sign_test_p(9, 1)  # двосторонній
    assert cmp.sign_test_p(10, 0) == pytest.approx(2 / 1024)
    assert cmp.sign_test_p(3, 0) == pytest.approx(0.25)  # три з трьох ще не доказ


def test_summarize_counts_and_rates():
    s = cmp.summarize(BASE)
    assert (s["n"], s["drafted"], s["refused"], s["errors"], s["flagged"], s["judged"]) == (8, 6, 1, 1, 2, 6)
    assert s["coverage"] == 1.0 and s["drafted_rate"] == 0.75 and s["refusal_rate"] == 0.125 and s["error_rate"] == 0.125
    assert s["fact_ok_rate"] == 0.5 and s["fact_ok_rate_n"] == 6
    assert s["style_45_rate"] == pytest.approx(1 / 3) and s["publishable_rate"] == pytest.approx(1 / 3)
    assert s["style_avg"] == 3.0 and s["tier_a_avg"] == pytest.approx(4 / 6) and s["tier_b_avg"] == pytest.approx(2 / 6)
    # самоперевірка моделі: з двох прапорців один справжній; з трьох фейкових драфтів прапорець мав лише один
    assert s["flag_precision"] == 0.5 and s["flag_recall"] == pytest.approx(1 / 3)
    assert s["failure_types"] == {"weak structure": 3, "factual distortion": 1, "hallucinated fact": 1}


def test_summarize_ignores_what_the_judge_did_not_score():
    rows = [R("a", "fb", fact="ok", style=4), R("b", "fb"), Row("c", "fb", draft="ok", eval="ERROR")]
    s = cmp.summarize(rows)
    assert (s["drafted"], s["judged"], s["judge_errors"]) == (3, 1, 1)
    assert s["coverage"] == pytest.approx(1 / 3) and s["fact_ok_rate"] == 1.0 and s["fact_ok_rate_n"] == 1
    empty = cmp.summarize([])
    assert empty["n"] == 0 and empty["fact_ok_rate"] is None and empty["coverage"] is None and empty["flag_recall"] is None


def test_scopes_split_by_channel():
    scopes = cmp.summarize_scopes(BASE)
    assert [scopes[k]["n"] for k in ("all", "fb", "x")] == [8, 4, 4]
    assert scopes["fb"]["fact_ok_rate"] == pytest.approx(1 / 3) and scopes["x"]["fact_ok_rate"] == pytest.approx(2 / 3)


# ---------- порівняння ----------

def test_compare_pairs_articles_and_names_the_changes():
    c = cmp.compare("hc-1", BASE, "hc-2", NEW)
    fact = c.delta("fact_ok_rate")
    assert (fact.base, fact.new, fact.pairs, fact.worse, fact.better, fact.p) == (0.5, pytest.approx(4 / 7), 6, 1, 1, 1.0)
    assert fact.worsening == pytest.approx(-(4 / 7 - 0.5))  # від'ємне — стало краще
    style = c.delta("style_45_rate")
    assert (style.pairs, style.worse, style.better) == (6, 1, 1)
    drafted = c.delta("drafted_rate")
    assert (drafted.base, drafted.new, drafted.worse, drafted.better) == (0.75, 0.875, 0, 1)
    tier_a = c.delta("tier_a_avg")  # менше — краще: f2 2→0 краще, f1 0→1 гірше, решта без змін
    assert (tier_a.higher_is_better, tier_a.worse, tier_a.better) == (False, 1, 1)
    assert c.delta("fact_ok_rate", "fb").pairs == 3 and c.delta("fact_ok_rate", "x").worse == 0
    assert c.same_articles and c.base["all"]["n"] == c.new["all"]["n"] == 8
    assert [(r["article_id"], r["what"]) for r in c.regressions] == [("f1", "з'явився фейк; Style 4 → 2")]
    assert [(r["article_id"], r["what"]) for r in c.improvements] == [("f2", "фейк зник; Style 2 → 4"),
                                                                     ("f4", "драфт з'явився")]
    assert c.regressions[0]["before"] == "fact ok · Style 4" and c.regressions[0]["after"] == "fact not ok · Style 2"
    assert c.improvements[1]["before"] == "відмова моделі"


def test_a_real_regression_is_significant_noise_is_not():
    base = [R(f"f{i}", "fb", fact="ok", style=4, a=0) for i in range(12)]
    worse = [R(f"f{i}", "fb", fact="not ok" if i < 9 else "ok", style=4, a=1 if i < 9 else 0) for i in range(12)]
    worse[11] = R("f11", "fb", fact="ok", style=4, a=0)
    c = cmp.compare("hc-1", base, "hc-2", worse)
    fact = c.delta("fact_ok_rate")
    assert (fact.worse, fact.better) == (9, 0) and fact.p == pytest.approx(2 / 512)
    assert fact.worsening == pytest.approx(9 / 12)
    noise = [R(f"f{i}", "fb", fact="not ok" if i in (0, 1) else "ok", style=4, a=0) for i in range(12)]
    assert cmp.compare("hc-1", base, "hc-2", noise).delta("fact_ok_rate").p == pytest.approx(0.5)  # 2 з 2 — ще не доказ


def test_compare_flags_different_article_sets():
    c = cmp.compare("hc-1", BASE, "hc-2", [r for r in NEW if r.article_id != "x1"])
    assert not c.same_articles
    assert c.delta("fact_ok_rate").pairs == 5  # попарно — лише спільні статті


def test_nothing_changed_means_no_findings():
    c = cmp.compare("hc-1", BASE, "hc-2", BASE)
    assert c.regressions == [] and c.improvements == []
    assert all(d.worse == d.better == 0 and d.p is None and d.delta == 0 for d in c.deltas["all"] if d.pairs)


# ---------- вибір бази й дані з бази (справжній Postgres) ----------

def test_baseline_and_previous_run_selection(seed_run, monkeypatch):
    seed_run("hc-1", started_at="2026-10-01T10:00:00Z")
    seed_run("hc-2", started_at="2026-10-08T10:00:00Z")
    seed_run("hc-3", started_at="2026-10-15T10:00:00Z", status="interrupted")  # обірваний — не база
    seed_run("hc-4", started_at="2026-10-22T10:00:00Z")
    assert cmp.previous_run("hc-4") == "hc-2" and cmp.previous_run("hc-2") == "hc-1" and cmp.previous_run("hc-1") is None
    assert cmp.baseline_run("hc-4") == "hc-1" and cmp.baseline_run("hc-1") is None  # найстаріший завершений
    monkeypatch.setenv("HEALTHCHECK_BASELINE_RUN", "hc-2")  # закріплена «прийнята» версія
    assert cmp.baseline_run("hc-4") == "hc-2" and cmp.baseline_run("hc-2") is None
    monkeypatch.setenv("HEALTHCHECK_BASELINE_RUN", "hc-3")  # закріплений, але не завершений
    assert cmp.baseline_run("hc-4") is None
    info = cmp.run_info("hc-2")
    assert info and info["model_version"] == "runpod-ep:draft_grounded" and info["status"] == "done"
    assert cmp.run_info("нема") is None


async def test_load_rows_joins_drafts_with_the_judges_verdicts(seed_run):
    seed_run("hc-1")
    key = "claude-haiku-5-5/medium:test"
    assert cmp.pending_evals("hc-1", key) == 2  # a1 і a2 мають текст
    assert cmp.pending_evals("hc-1", key, retry_errors=False) == 2
    first, second = hc_eval.pending_cases("hc-1", key)
    hc_eval.save_eval(hc_eval._row(first, key, outcome="EVALUATED", fact_safety="ok", tier_a=0, tier_b=1, style=4,
                                   failure_types=["weak structure"], status="Approved after edit", source_captured="yes",
                                   cost_usd=0.002))
    hc_eval.save_eval(hc_eval._row(second, key, outcome="ERROR", error="boom"))
    assert cmp.pending_evals("hc-1", key) == 1  # ERROR оцінкою не вважається...
    assert cmp.pending_evals("hc-1", key, retry_errors=False) == 0  # ...але автоматичний супровід його не повторює
    rows = {r.article_id: r for r in cmp.load_rows("hc-1", key)}
    assert sorted(rows) == ["a1", "a2", "x1", "x2"] and rows["a1"].channel == "fb" and rows["a1"].title == "Title a1"
    a1 = rows["a1"]
    assert (a1.draft, a1.eval, a1.fact_safety, a1.tier_b, a1.style, a1.failure_types, a1.status, a1.cost_usd) == \
        ("ok", "EVALUATED", "ok", 1, 4, ["weak structure"], "Approved after edit", pytest.approx(0.002))
    assert (rows["a2"].draft, rows["a2"].eval, rows["a2"].fact_safety) == ("needs_manual_review", "ERROR", None)
    assert (rows["x1"].draft, rows["x1"].eval, rows["x2"].draft) == ("refused", None, "error")
    other = {r.article_id: r for r in cmp.load_rows("hc-1", "інший-суддя")}
    assert other["a1"].eval is None and other["a1"].fact_safety is None  # оцінки іншого судді не підмішуються
    s = cmp.summarize(list(rows.values()))
    assert (s["drafted"], s["refused"], s["errors"], s["judged"], s["judge_errors"], s["coverage"]) == (2, 1, 1, 1, 1, 0.5)
    assert cmp.load_rows("нема-такого", key)[0].draft == "missing"  # прогін без рядка по статті — missing, а не зникла стаття


# ---------- ланцюжок і ендпоінт ----------

def _judge(seed_run_id: str, verdicts: dict[str, tuple[str, int]], key: str | None = None) -> str:
    """Записати оцінки a1/a2 прогону: {article_id: (fact_safety, style)}; повертає ключ судді."""
    key = key or hc_eval.current_judge_key()
    for case in hc_eval.pending_cases(seed_run_id, key):
        fact, style = verdicts[case.article_id]
        hc_eval.save_eval(hc_eval._row(case, key, outcome="EVALUATED", fact_safety=fact, style=style,
                                       tier_a=0 if fact == "ok" else 1, tier_b=0, failure_types=[]))
    return key


async def test_compare_runs_pairs_a_run_with_the_previous_one(seed_run):
    seed_run("hc-1", started_at="2026-10-01T10:00:00Z", model="runpod-old:draft_grounded")
    seed_run("hc-2", started_at="2026-10-08T10:00:00Z", model="runpod-new:draft_grounded")
    assert await hc_flow.compare_runs("hc-1") is None  # перший прогін — бази нема
    _judge("hc-1", {"a1": ("ok", 4), "a2": ("not ok", 2)})
    key = _judge("hc-2", {"a1": ("not ok", 2), "a2": ("not ok", 2)})
    c = await hc_flow.compare_runs("hc-2")
    assert c and (c.base_run, c.new_run, c.same_articles, c.warnings) == ("hc-1", "hc-2", True, [])
    fact = c.delta("fact_ok_rate")
    assert (fact.base, fact.new, fact.pairs, fact.worse, fact.better) == (0.5, 0.0, 2, 1, 0)
    assert [r["article_id"] for r in c.regressions] == ["a1"] and c.regressions[0]["after"] == "fact not ok · Style 2"
    assert c.meta["judge"] == key and c.meta["base"]["model_version"] == "runpod-old:draft_grounded"
    assert c.meta["new"]["model_version"] == "runpod-new:draft_grounded"
    explicit = await hc_flow.compare_runs("hc-2", "hc-1", key)
    assert explicit and explicit.delta("fact_ok_rate").worse == 1


async def test_compare_warns_when_the_judge_has_not_scored_everything(seed_run, monkeypatch):
    seed_run("hc-1", started_at="2026-10-01T10:00:00Z")
    seed_run("hc-2", started_at="2026-10-08T10:00:00Z")
    _judge("hc-1", {"a1": ("ok", 4), "a2": ("ok", 4)})
    c = await hc_flow.compare_runs("hc-2")  # hc-2 цим суддею ще не оцінений
    assert c and any("hc-2: 2 драфтів без оцінки" in w for w in c.warnings)
    assert any("hc-2: оцінено лише 0%" in w for w in c.warnings) and not any("hc-1:" in w for w in c.warnings)
    evaluated = []

    async def fake_evaluate(run_id, judge=None, **kw):
        evaluated.append(run_id)
        _judge(run_id, {"a1": ("ok", 4), "a2": ("ok", 4)})
        return {}

    monkeypatch.setattr(hc_eval, "evaluate_run", fake_evaluate)
    done = await hc_flow.compare_runs("hc-2", evaluate_missing=True)  # ланцюжок дооцінює те, чого бракує
    assert evaluated == ["hc-2"] and done and done.warnings == []
    await hc_flow.compare_runs("hc-2", judge_key="чужий-суддя", evaluate_missing=True)
    assert evaluated == ["hc-2"]  # чужого суддю не підміняємо: дооцінки не буде


def test_compare_endpoint(monkeypatch):
    known = {"hc-1", "hc-2"}
    monkeypatch.setattr(cmp, "run_info", lambda rid: {"run_id": rid} if rid in known else None)
    result = {}

    async def fake_compare(run_id, base_id=None, judge_key=None, **kw):
        return result.get("c")

    monkeypatch.setattr(hc_flow, "compare_runs", fake_compare)
    with TestClient(app) as c:
        assert c.get("/health-check/compare?run=нема").status_code == 404
        assert c.get("/health-check/compare?run=hc-2&base=нема").status_code == 404
        first = c.get("/health-check/compare?run=hc-1").json()
        assert first["base_run"] is None and "перший" in first["note"]
        result["c"] = cmp.compare("hc-1", BASE, "hc-2", NEW)
        body = c.get("/health-check/compare?run=hc-2&base=hc-1").json()
    fact = next(d for d in body["deltas"]["all"] if d["key"] == "fact_ok_rate")
    assert (body["base_run"], body["new_run"], fact["worse"], fact["better"], fact["p"]) == ("hc-1", "hc-2", 1, 1, 1.0)
    assert fact["delta"] == pytest.approx(4 / 7 - 0.5) and fact["worsening"] == pytest.approx(-(4 / 7 - 0.5))
    assert body["regressions"][0]["article_id"] == "f1" and body["base"]["fb"]["n"] == 4
