"""Health Check, пункт 3: агент-оцінювач — вхід, розбір відповіді, виклик Anthropic, запис і повторне використання оцінок.
Без мережі: суддя підмінений, а SDK Anthropic в одному тесті ходить у підроблений транспорт. Тести з пометкою pgdb
потребують справжнього Postgres (TEST_DATABASE_URL), інакше пропускаються."""
import os

os.environ.setdefault("NOTION_TOKEN", "secret_xxx")
os.environ.setdefault("WEBHOOK_SECRET", "t")
os.environ.setdefault("NOTION_DATABASE_ID", "0" * 32)
os.environ.setdefault("RUNPOD_ENDPOINT_ID", "test")
os.environ.setdefault("RUNPOD_API_KEY", "test")
os.environ.setdefault("COMMS_DATABASE_URL", "")

import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import anthropic  # noqa: E402
import httpx2  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import hc_eval, hc_flow, healthcheck, pg  # noqa: E402
from app.main import app  # noqa: E402

REPLY = """RESULT
page: hc://hc-1/a1
id8: hc-1/a1
channel: fb
outcome: EVALUATED
relevance: yes
fact_safety: not ok
tier_a: 1
tier_b: 1
unverifiable: 0
pipeline_flags: confirmed 1, false_alarm 1
source_captured: partial
style: 3
defects: major 2, minor 3, recurring 0
failure_types: factual distortion | weak structure | language / adaptation problem
status_written: Approved after edit
article_method: file
article_url: https://x/a1
need_article_reason: -
second_opinion_reason: -
notion_written: no

CLAIMS
U1 | Росія втратила 12 літаків. | TIER_A | distorted | «lost 2 aircraft»
U2 | Міністр заявив, що план діє. | SUPPORTED | - | «the minister said the plan is in effect»
U3 | Трамп підтримав. 1/ | TIER_B | - | «Trump reportedly backed»

DEFECTS
1 | Це не X. Це Y. | «не X, а Y» у власному голосі | major | weak structure
2 | Він каже | «Він каже» замість «Прізвище: теза» | minor | language / adaptation problem

CORRECTED POST
Перший абзац.
Другий абзац.

COMMENT
**Feedback for the model**
1. `Росія втратила 12 літаків` — у статті: **2 літаки**.
2. [agent review · draft-eval · 2026-10-21 · source captured: partial · Tier B: 1]
"""


def _case(aid="a1", outcome="ok", text="Пост про літаки.", extraction=None) -> hc_eval.EvalCase:
    return hc_eval.EvalCase(run_id="hc-1", article_id=aid, channel="fb", lang="uk", model_version="runpod-ep:draft_grounded",
                            draft_outcome=outcome, draft_text=text, title=f"Title {aid}", url=f"https://x/{aid}",
                            source="Kyiv Post", article_text="Russia lost 2 aircraft.\n\nSecond paragraph.",
                            extraction=extraction or {})


# ---------- інструкція й вхід ----------

def test_instruction_is_antons_and_matches_the_parser():
    text = hc_eval.instruction()
    assert text.startswith("# Інструкція агентові: оцінити один драфт")
    assert "Версія 3 від 2026-10-13" in text  # той самий файл, що Антон давав агентам
    # поля RESULT, які читає розбір, є в шаблоні кроку 13 самої інструкції
    template = text.split("## Крок 13. Звіт")[1]
    for key in ("outcome:", "relevance:", "fact_safety:", "tier_a:", "tier_b:", "unverifiable:", "pipeline_flags:",
                "source_captured:", "style:", "defects:", "failure_types:", "status_written:", "second_opinion_reason:"):
        assert key in template, key
    for section in ("CLAIMS", "DEFECTS", "CORRECTED POST", "COMMENT"):
        assert section in template
    assert hc_eval.system_blocks()[0]["text"] == text  # інструкція йде без жодної правки; вставка — окремим блоком


def test_system_blocks_cache_once_after_the_static_prefix():
    blocks = hc_eval.system_blocks()
    assert [b["text"] for b in blocks] == [hc_eval.instruction(), hc_eval.ADAPTER]
    assert [b.get("cache_control") for b in blocks] == [None, {"type": "ephemeral"}]


def test_user_message_recreates_the_notion_page_for_a_checked_draft():
    ex = {"facts_used": [{"claim": "літаки втрачено", "verbatim_quote": "lost 2 aircraft"}],
          "facts_rejected": [{"claim": "12 літаків", "verbatim_quote": "12 aircraft"}]}
    msg = hc_eval.build_user_message(_case(extraction=ex))
    assert msg.startswith("PAGE: hc://hc-1/a1\nMODE: dry\nARTICLE_URL: https://x/a1\n")
    assert "ARTICLE_TEXT:\n<<<\nRussia lost 2 aircraft.\n\nSecond paragraph.\n>>>" in msg
    assert "Draft: Title a1\nSource: Title a1 — https://x/a1\nStatus: New draft" in msg
    assert "run_id: hc-1 · model: runpod-ep:draft_grounded · канал: fb · мова: uk" in msg
    assert ("▸ Факти, з яких написано пост (1) — кожен підтверджено цитатою зі статті\n"
            "  • літаки втрачено — «lost 2 aircraft»") in msg
    assert "▸ Відкинуті факти (1)" in msg
    assert "Що не пройшло перевірку" not in msg and "НЕ ПЕРЕВІРЕНО" not in msg


def test_user_message_for_a_draft_that_failed_the_models_own_check():
    ex = {"verification": {"judge_failures": [{"text": "Росія втратила 12 літаків.", "offending_span": "12",
                                               "verdict": "DISTORTED", "reason": "у фактах 2"}]}}
    msg = hc_eval.build_user_message(_case(outcome="needs_manual_review", extraction=ex))
    assert "Draft: ⚠ Не пройшов перевірку: Title a1" in msg and "Status: Needs fact-check" in msg
    assert "⚠ НЕ ПЕРЕВІРЕНО." in msg
    assert "▸ Що не пройшло перевірку (1)\n  • «Росія втратила 12 літаків.» → «12» — DISTORTED: у фактах 2" in msg


# ---------- розбір відповіді ----------

def test_parse_full_reply():
    v = hc_eval.parse_verdict(REPLY)
    assert (v.outcome, v.relevance, v.fact_safety, v.tier_a, v.tier_b, v.unverifiable) == ("EVALUATED", "yes", "not ok", 1, 1, 0)
    assert (v.flags_confirmed, v.flags_false, v.source_captured, v.style) == (1, 1, "partial", 3)
    assert v.defects == {"major": 2, "minor": 3, "recurring": 0}
    assert v.failure_types == ["factual distortion", "weak structure", "language / adaptation problem"]
    assert v.status == "Approved after edit" and v.second_opinion_reason is None
    assert [(c["id"], c["verdict"], c["subtype"]) for c in v.claims] == [("U1", "TIER_A", "distorted"), ("U2", "SUPPORTED", ""),
                                                                      ("U3", "TIER_B", "")]
    assert v.claims[0]["quote"] == "«lost 2 aircraft»" and v.claims[2]["text"] == "Трамп підтримав. 1/"
    assert [(d["n"], d["level"], d["failure_type"]) for d in v.defect_list] == [("1", "major", "weak structure"),
                                                                              ("2", "minor", "language / adaptation problem")]
    assert v.corrected_post == "Перший абзац.\nДругий абзац."
    assert v.comment and v.comment.startswith("**Feedback for the model**") and "agent review" in v.comment
    assert v.problems == []


def test_parse_tolerates_markdown_decoration():
    decorated = "Ось оцінка:\n```text\n" + REPLY.replace("RESULT\n", "**RESULT**\n").replace("outcome:", "**outcome:**") \
        .replace("CLAIMS\n", "## CLAIMS\n").replace("COMMENT\n", "COMMENT:\n") + "```\n"
    v = hc_eval.parse_verdict(decorated)
    assert (v.outcome, v.fact_safety, v.style, len(v.claims), len(v.defect_list)) == ("EVALUATED", "not ok", 3, 3, 2)
    assert v.corrected_post == "Перший абзац.\nДругий абзац."


def test_parse_dashes_mean_empty():
    raw = "RESULT\noutcome: SECOND_OPINION\nfact_safety: -\ntier_a: 0\nunverifiable: 1\nstyle: 4\nfailure_types: -\n" \
          "status_written: Second opinion\nsecond_opinion_reason: не вдалося перевірити U3\n\nCLAIMS\n-\n\nDEFECTS\n-\n\n" \
          "CORRECTED POST\n-\n\nCOMMENT\n-\n"
    v = hc_eval.parse_verdict(raw)
    assert (v.outcome, v.fact_safety, v.style) == ("SECOND_OPINION", None, 4)
    assert (v.failure_types, v.claims, v.defect_list) == ([], [], [])
    assert v.corrected_post is None and v.comment is None and v.second_opinion_reason == "не вдалося перевірити U3"
    assert v.problems == []  # SECOND_OPINION без fact_safety — штатно: нуль Tier A ще не означає ok


def test_fact_safety_follows_tier_a_rule_not_the_models_word():
    wrong_ok = REPLY.replace("fact_safety: not ok", "fact_safety: ok")  # Tier A = 1, а суддя написав ok
    v = hc_eval.parse_verdict(wrong_ok)
    assert v.fact_safety == "not ok" and "суперечить tier_a=1" in v.problems[0]
    clean = REPLY.replace("tier_a: 1", "tier_a: 0").replace("fact_safety: not ok", "fact_safety: not ok")
    v = hc_eval.parse_verdict(clean)
    assert v.fact_safety == "ok" and "tier_a=0" in v.problems[0]
    no_field = REPLY.replace("fact_safety: not ok\n", "").replace("tier_a: 1\n", "")  # tier_a береться з CLAIMS
    v = hc_eval.parse_verdict(no_field)
    assert (v.tier_a, v.fact_safety, v.problems) == (1, "not ok", [])
    so = REPLY.replace("outcome: EVALUATED", "outcome: SECOND_OPINION")  # Tier A ≥ 1 лишається not ok
    assert hc_eval.parse_verdict(so).fact_safety == "not ok"


def test_parse_flags_incomplete_evaluation():
    v = hc_eval.parse_verdict("RESULT\noutcome: EVALUATED\nrelevance: yes\n")
    assert v.problems == ["EVALUATED без fact_safety", "EVALUATED без style"]


@pytest.mark.parametrize("raw, message", [("Вибач, не можу.", "немає блоку RESULT"),
                                          ("RESULT\noutcome: MAYBE\n", "невідомий outcome"),
                                          ("RESULT\npage: x\n", "невідомий outcome")])
def test_parse_rejects_garbage(raw, message):
    with pytest.raises(hc_eval.ParseError, match=message):
        hc_eval.parse_verdict(raw)


def test_cost_uses_cache_prices():
    reply = hc_eval.JudgeReply("", input_tokens=1000, cache_read_tokens=20000, cache_write_tokens=0, output_tokens=2000)
    assert hc_eval.cost_usd("claude-haiku-5-5", reply) == pytest.approx(0.0013)
    first = hc_eval.JudgeReply("", input_tokens=1000, cache_write_tokens=20000, output_tokens=2000)  # запис у кеш дорожчий
    assert hc_eval.cost_usd("claude-haiku-5-5", first) == pytest.approx((100 + 2500 + 1000) / 1e6)
    assert hc_eval.cost_usd("claude-невідома", reply) is None


# ---------- виклик Anthropic ----------

def _sdk_reply(text=REPLY, stop="end_turn", **usage):
    u = dict(input_tokens=1200, output_tokens=3000, cache_read_input_tokens=20000, cache_creation_input_tokens=None) | usage
    return SimpleNamespace(stop_reason=stop, stop_details=SimpleNamespace(category="cyber"), usage=SimpleNamespace(**u),
                           content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])


def _client(create):
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_judge_sends_the_documented_request_through_the_real_sdk():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(path=request.url.path, body=json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "stop_reason": "end_turn",
            "stop_sequence": None, "content": [{"type": "text", "text": REPLY}],
            "usage": {"input_tokens": 1200, "output_tokens": 3000, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 20000}})

    sdk = anthropic.Anthropic(api_key="k", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    reply = hc_eval.AnthropicJudge(client=sdk).evaluate(_case())
    body = seen["body"]
    assert seen["path"] == "/v1/messages"
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == hc_eval.MAX_TOKENS
    assert body["output_config"] == {"effort": "medium"}
    assert not {"temperature", "top_p", "top_k", "thinking", "tools", "stream"} & set(body)  # Haiku 5.5 приймає лише дефолти
    assert body["system"][0]["text"] == hc_eval.instruction() and body["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert [m["role"] for m in body["messages"]] == ["user"] and "PAGE: hc://hc-1/a1" in body["messages"][0]["content"]
    assert (reply.text, reply.input_tokens, reply.cache_read_tokens, reply.output_tokens) == (REPLY, 1200, 20000, 3000)


def test_judge_key_changes_with_model_effort_and_prompt(monkeypatch, tmp_path):
    base = hc_eval.AnthropicJudge(client=_client(None)).key
    assert base.startswith("claude-haiku-5-5/medium:")
    assert hc_eval.AnthropicJudge(client=_client(None), model="claude-sonnet-5-5").key != base
    with monkeypatch.context() as m:
        m.setattr(hc_eval, "EFFORT", "high")
        assert hc_eval.AnthropicJudge(client=_client(None)).key != base
    other = tmp_path / "instruction.txt"
    other.write_text("інша інструкція", encoding="utf-8")
    with monkeypatch.context() as m:
        m.setattr(hc_eval, "INSTRUCTION_FILE", other)
        assert hc_eval.AnthropicJudge(client=_client(None)).key != base
    assert hc_eval.AnthropicJudge(client=_client(None)).key == base  # вигляд ключа стабільний


def test_judge_failure_modes():
    ok = hc_eval.AnthropicJudge(client=_client(lambda **kw: _sdk_reply(cache_creation_input_tokens=7)))
    assert ok.evaluate(_case()).cache_write_tokens == 7
    with pytest.raises(hc_eval.JudgeError, match="відмовився: cyber"):
        hc_eval.AnthropicJudge(client=_client(lambda **kw: _sdk_reply(stop="refusal"))).evaluate(_case())
    with pytest.raises(hc_eval.JudgeError, match="обрізано"):
        hc_eval.AnthropicJudge(client=_client(lambda **kw: _sdk_reply(stop="max_tokens"))).evaluate(_case())

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def failing(error):
        def create(**kw):
            raise error
        return hc_eval.AnthropicJudge(client=_client(create))

    unauthorized = anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=request), body=None)
    with pytest.raises(hc_eval.JudgeFatal, match="AuthenticationError"):
        failing(unauthorized).evaluate(_case())
    no_credit = anthropic.BadRequestError("Your credit balance is too low", response=httpx2.Response(400, request=request),
                                          body=None)
    with pytest.raises(hc_eval.JudgeFatal, match="закінчились кошти"):
        failing(no_credit).evaluate(_case())
    other_400 = anthropic.BadRequestError("temperature: not allowed", response=httpx2.Response(400, request=request), body=None)
    with pytest.raises(anthropic.BadRequestError):  # звичайна 400 — не fatal: помилка конкретної оцінки
        failing(other_400).evaluate(_case())


def test_enabled_needs_key_and_switch(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HEALTHCHECK_EVAL", "1")
    assert not hc_eval.enabled()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert hc_eval.enabled()
    monkeypatch.setenv("HEALTHCHECK_EVAL", "0")
    assert not hc_eval.enabled()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        hc_eval.get_judge()


# ---------- запис і прогін оцінки (справжній Postgres) ----------

class FakeJudge:
    key = "claude-haiku-5-5/medium:test"

    def __init__(self, replies=None):
        self.replies, self.calls = replies or {}, []

    def evaluate(self, case):
        self.calls.append(case.article_id)
        reply = self.replies.get(case.article_id, REPLY)
        if isinstance(reply, Exception):
            raise reply
        return hc_eval.JudgeReply(reply, input_tokens=1000, cache_read_tokens=20000, output_tokens=2000)


def test_schema_is_in_step_with_columns(pgdb):
    healthcheck.init_db()
    healthcheck.init_db()  # повторний старт бекенду нічого не ламає
    with pg._connect() as c:
        cols = {r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'hc_evals' "
                                        "AND table_schema = current_schema()")}
    assert cols == set(hc_eval.COLS) | {"created_at"}


def test_pending_cases_take_only_drafts_with_text_and_not_yet_judged(seed_run):
    seed_run(extraction={"facts_used": [{"claim": "c", "verbatim_quote": "q"}]})
    cases = hc_eval.pending_cases("hc-1", "k1")
    assert [(c.article_id, c.draft_outcome, c.lang, c.channel) for c in cases] == [("a1", "ok", "uk", "fb"),
                                                                                 ("a2", "needs_manual_review", "uk", "fb")]
    assert cases[0].extraction == {"facts_used": [{"claim": "c", "verbatim_quote": "q"}]}
    assert cases[0].article_text == "Текст статті a1" and cases[0].model_version == "runpod-ep:draft_grounded"
    hc_eval.save_eval(hc_eval._row(cases[0], "k1", outcome="EVALUATED"))
    assert [c.article_id for c in hc_eval.pending_cases("hc-1", "k1")] == ["a2"]
    assert [c.article_id for c in hc_eval.pending_cases("hc-1", "k2")] == ["a1", "a2"]  # інший суддя — оцінки окремі
    assert [c.article_id for c in hc_eval.pending_cases("hc-1", "k1", force=True)] == ["a1", "a2"]
    assert [c.article_id for c in hc_eval.pending_cases("hc-1", "k1", force=True, only={"a2"})] == ["a2"]
    hc_eval.save_eval(hc_eval._row(cases[1], "k1", outcome="ERROR", error="boom"))
    assert [c.article_id for c in hc_eval.pending_cases("hc-1", "k1")] == ["a2"]  # ERROR повторюємо


async def test_evaluate_run_stores_everything_and_judges_the_first_draft_alone(seed_run):
    seed_run()
    judge = FakeJudge({"a2": REPLY.replace("style: 3", "style: 5").replace("tier_a: 1", "tier_a: 0")
                       .replace("fact_safety: not ok", "fact_safety: ok")})
    summary = await hc_eval.evaluate_run("hc-1", judge)
    assert judge.calls[0] == "a1" and sorted(judge.calls) == ["a1", "a2"]
    assert summary["outcomes"] == {"EVALUATED": 2} and summary["cost_usd"] == pytest.approx(0.0026)
    rows = {r["article_id"]: r for r in hc_eval.list_evals("hc-1")}
    assert rows["a1"]["fact_safety"] == "not ok" and rows["a1"]["style"] == 3 and rows["a1"]["tier_a"] == 1
    assert rows["a2"]["fact_safety"] == "ok" and rows["a2"]["style"] == 5
    assert rows["a1"]["failure_types"] == ["factual distortion", "weak structure", "language / adaptation problem"]
    assert rows["a1"]["channel"] == "fb" and rows["a1"]["title"] == "Title a1" and rows["a1"]["status"] == "Approved after edit"
    with pg._connect() as c:
        raw, claims, tokens = c.execute("SELECT raw, claims, cache_read_tokens FROM hc_evals WHERE article_id = 'a1'").fetchone()
    assert raw == REPLY and claims[0]["verdict"] == "TIER_A" and tokens == 20000
    assert hc_eval.list_evals("hc-1", "інший-ключ") == []


async def test_evaluate_run_resumes_and_does_not_pay_twice(seed_run):
    seed_run()
    first = FakeJudge({"a2": RuntimeError("мережа")})
    summary = await hc_eval.evaluate_run("hc-1", first)
    assert summary["outcomes"] == {"EVALUATED": 1, "ERROR": 1}
    errors = [r for r in hc_eval.list_evals("hc-1") if r["outcome"] == "ERROR"]
    assert [(r["article_id"], r["error"]) for r in errors] == [("a2", "RuntimeError: мережа")]
    again = FakeJudge()
    await hc_eval.evaluate_run("hc-1", again)
    assert again.calls == ["a2"]  # оцінене не чіпаємо, збій повторюємо
    await hc_eval.evaluate_run("hc-1", again)
    assert again.calls == ["a2"]  # усе оцінено — нічого не робимо
    await hc_eval.evaluate_run("hc-1", again, force=True)
    assert sorted(again.calls) == ["a1", "a2", "a2"]


async def test_unparsable_answer_is_stored_as_error_with_raw_text(seed_run):
    seed_run()
    await hc_eval.evaluate_run("hc-1", FakeJudge({"a1": "Вибач, не можу оцінити цей текст."}))
    with pg._connect() as c:
        outcome, error, raw, cost = c.execute(
            "SELECT outcome, error, raw, cost_usd FROM hc_evals WHERE article_id = 'a1'").fetchone()
    assert outcome == "ERROR" and "ParseError" in error and raw == "Вибач, не можу оцінити цей текст." and cost > 0


async def test_same_draft_text_is_not_judged_again_in_a_later_run(seed_run):
    seed_run(texts={"a1": "Той самий пост", "a2": "Інший пост"})
    judge = FakeJudge()
    await hc_eval.evaluate_run("hc-1", judge)
    seed_run(run_id="hc-2", texts={"a1": "Той самий пост", "a2": "a2 тепер переписано"})
    second = FakeJudge()
    summary = await hc_eval.evaluate_run("hc-2", second)
    assert second.calls == ["a2"] and summary["outcomes"] == {"REUSED": 1, "EVALUATED": 1}
    with pg._connect() as c:
        reused = c.execute("SELECT reused_from, cost_usd, input_tokens, fact_safety FROM hc_evals "
                           "WHERE run_id = 'hc-2' AND article_id = 'a1'").fetchone()
    assert reused == ("hc-1", 0.0, 0, "not ok")


async def test_fatal_judge_error_stops_the_whole_evaluation(seed_run):
    seed_run()
    judge = FakeJudge({"a1": hc_eval.JudgeFatal("AuthenticationError: invalid x-api-key")})
    with pytest.raises(hc_eval.JudgeFatal, match="invalid x-api-key"):
        await hc_eval.evaluate_run("hc-1", judge)
    assert judge.calls == ["a1"] and hc_eval.list_evals("hc-1") == []  # решта запитів не пішла, помилку не записано як оцінку


# ---------- ланцюжок і ендпоінти ----------

async def test_after_run_never_raises_and_does_not_overlap(monkeypatch):
    calls, reports = [], []

    async def fake_eval(run_id, judge=None, *, force=False):
        calls.append((run_id, force))
        assert hc_flow.is_busy(run_id)
        if run_id == "boom":
            raise RuntimeError("немає ключа")

    async def fake_report(run_id, base_id=None, judge_key=None, **kw):
        reports.append((run_id, judge_key))

    monkeypatch.setattr(hc_eval, "evaluate_run", fake_eval)
    monkeypatch.setattr(hc_flow, "make_report", fake_report)
    await hc_flow.after_run("hc-1", force=True)
    await hc_flow.after_run("boom")  # збій оцінки не кидається нагору
    assert calls == [("hc-1", True), ("boom", False)] and not hc_flow.is_busy("hc-1") and not hc_flow.is_busy("boom")
    assert reports == [("hc-1", None)]  # звіт будується лише після успішної оцінки


async def test_run_hands_a_finished_run_to_the_evaluator_only_when_enabled(monkeypatch):
    from test_healthcheck import REFS, FakeModel, FakeStore  # той самий модуль, що вже завантажив pytest

    handed = []

    async def fake_after(run_id, **kw):
        handed.append(run_id)

    monkeypatch.setattr(hc_flow, "after_run", fake_after)
    for enabled in (False, True):
        store = FakeStore(REFS[:1])
        store.install(monkeypatch)
        monkeypatch.setattr(hc_eval, "enabled", lambda e=enabled: e)
        run_id, model = healthcheck.start("manual", FakeModel())
        await healthcheck.run(run_id, model)
        assert store.finished == [(run_id, "done", None)]  # оцінка не змінює статус прогону
    assert len(handed) == 1


def test_evaluate_and_evals_endpoints(monkeypatch):
    started = []

    async def fake_after(run_id, *, judge=None, force=False):
        started.append((run_id, judge.key, force))

    real_get_judge = hc_eval.get_judge
    monkeypatch.setattr(hc_flow, "after_run", fake_after)
    monkeypatch.setattr(healthcheck, "get_run", lambda rid: {"run_id": rid} if rid == "hc-1" else None)
    monkeypatch.setattr(hc_eval, "get_judge", lambda model=None: FakeJudge())
    monkeypatch.setattr(hc_eval, "list_evals", lambda rid, judge=None: [{"article_id": "a1", "judge": judge}])
    secret = {"x-webhook-secret": "t"}
    with TestClient(app) as c:
        assert c.post("/health-check/runs/hc-1/evaluate").status_code == 401
        assert c.post("/health-check/runs/nope/evaluate", headers=secret).status_code == 404
        r = c.post("/health-check/runs/hc-1/evaluate", headers=secret, json={"force": True})
        assert (r.status_code, r.json()) == (202, {"run_id": "hc-1", "judge": FakeJudge.key})
        monkeypatch.setattr(hc_flow, "is_busy", lambda rid: True)
        assert c.post("/health-check/runs/hc-1/evaluate", headers=secret).status_code == 409
        monkeypatch.setattr(hc_flow, "is_busy", lambda rid: False)
        monkeypatch.setattr(hc_eval, "get_judge", real_get_judge)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert c.post("/health-check/runs/hc-1/evaluate", headers=secret).status_code == 503  # ключа нема
        assert c.get("/health-check/runs/hc-1/evals?judge=k").json() == [{"article_id": "a1", "judge": "k"}]
        assert c.get("/health-check/runs/nope/evals").status_code == 404
    assert started == [("hc-1", FakeJudge.key, True)]
