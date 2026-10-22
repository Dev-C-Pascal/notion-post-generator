"""Health Check, пункт 4: чи можна вірити судді. Агент-оцінювач (hc_eval, інструкція Антона + дешева модель Anthropic) оцінює
ті самі драфти, які вже оцінили люди в таблиці MVP, і ми рахуємо, наскільки його слово збігається з людським.

Люди: рядки таблиці MVP (NOTION_DATABASE_ID), де заповнені всі три колонки — Article relevance (yes/no), Fact safety
(ok / not ok), Style score (1–5). Текст драфта — з postgen drafts за notion_page_id (той, що бекенд відправив у Notion),
текст статті — з бази Андрія (comms.fetch_by_ids). Усе це копіюється в hc_human один раз: звірку можна повторити новим
суддею, навіть коли comms недоступна чи статтю там уже прибрали. Оцінки людей при кожному зборі оновлюються з Notion.

Метрики (metrics — чиста функція):
  • збіг по Fact safety серед драфтів, де суддя дав вердикт; ціль ≥ 80% (HEALTHCHECK_CALIBRATION_TARGET); поруч — базова
    лінія «завжди not ok» (частка not ok у людей): суддя, що не кращий за неї, нічого не розрізняє;
  • матриця помилок і kappa Коена (збіг з поправкою на випадковий);
  • «можна публікувати» (fact ok + Style 4–5; у людей ще й relevance = yes): збіг, precision і recall судді;
  • Style: точний збіг і ±1 бал, середнє в людей і в судді;
  • те саме по каналах (FB / X).

Збіг нижче цілі — повідомлення через notify (HEALTHCHECK_CALIBRATION_MENTION — кого згадати, напр. @username Антона):
тоді цифрам Health Check не варто довіряти, доки не підправлять інструкцію чи модель судді.

Запуск (з сервера; ≈ 300 викликів судді — платно, див. CONTEXT.md §17):
    docker compose exec -T app python -m app.hc_calibrate            # зібрати → оцінити → звіт → Notion → повідомлення
    docker compose exec -T app python -m app.hc_calibrate collect    # лише зібрати пари, без витрат
    docker compose exec -T app python -m app.hc_calibrate report     # звіт з уже наявних оцінок, без нових викликів
або POST /health-check/calibration і GET /health-check/calibration.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg

from . import comms, hc_eval, hc_report, notify, notion, pg, pipeline
from .hc_fmt import CHANNEL, num, pct

log = logging.getLogger("hc_calibrate")

RELEVANCE_PROP, FACT_PROP, STYLE_PROP = "Article relevance", "Fact safety", "Style score"
DEFAULT_TARGET = 0.80
RUN_ID = "calibration"  # службовий run_id у PAGE (hc://calibration/<article_id>): у hc_runs такого прогону немає
SHOWN_DISAGREEMENTS = 15

_pool = ThreadPoolExecutor(max_workers=hc_eval.PARALLEL, thread_name_prefix="hc-calibrate")
_busy = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS hc_human (
    notion_page_id  TEXT PRIMARY KEY,         -- рядок таблиці MVP, без дефісів
    article_id      TEXT NOT NULL,
    channel         TEXT NOT NULL,            -- fb | x
    lang            TEXT NOT NULL,            -- uk | en
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    article_text    TEXT NOT NULL,            -- копія з comms.core.article: звірка не залежить від comms
    source          TEXT NOT NULL,
    draft_text      TEXT NOT NULL,            -- те, що бекенд відправив у Notion (postgen drafts.text)
    draft_outcome   TEXT NOT NULL,            -- ok | needs_manual_review
    extraction      JSONB,
    model_version   TEXT NOT NULL,
    relevance       TEXT NOT NULL,            -- оцінки людини: yes | no
    fact_safety     TEXT NOT NULL,            -- ok | not ok
    style           INTEGER NOT NULL,         -- 1–5
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hc_human_evals (
    notion_page_id  TEXT NOT NULL REFERENCES hc_human(notion_page_id),
    judge_key       TEXT NOT NULL,
    draft_sha       TEXT NOT NULL,
    outcome         TEXT NOT NULL,            -- EVALUATED | SECOND_OPINION | … | ERROR
    fact_safety     TEXT,
    tier_a          INTEGER,
    tier_b          INTEGER,
    style           INTEGER,
    failure_types   JSONB,
    problems        JSONB,
    raw             TEXT,
    error           TEXT,
    cost_usd        REAL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (notion_page_id, judge_key)
);
CREATE TABLE IF NOT EXISTS hc_calibrations (
    id          BIGSERIAL PRIMARY KEY,
    judge_key   TEXT NOT NULL,
    agreement   REAL,                         -- збіг по fact safety
    target      REAL NOT NULL,
    decided     INTEGER NOT NULL,             -- на скількох драфтах суддя дав fact safety
    metrics     JSONB NOT NULL,
    markdown    TEXT NOT NULL,
    notion_url  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def target() -> float:
    return float(os.environ.get("HEALTHCHECK_CALIBRATION_TARGET") or DEFAULT_TARGET)


def init_db() -> None:
    with pg._connect() as c:
        c.execute(SCHEMA)


# ---------- збір пар «драфт — людська оцінка» ----------

def _norm(page_id: str | None) -> str:
    return (page_id or "").replace("-", "")


def _style(name: str | None) -> int | None:
    m = re.search(r"[1-5]", name or "")
    return int(m.group()) if m else None


def human_labels(pages: list[dict]) -> dict[str, dict[str, Any]]:
    """Рядки MVP → {page_id: {relevance, fact_safety, style}}; лише повністю оцінені (усі три колонки)."""
    out = {}
    for p in pages:
        rel = (notion.select_value(p, RELEVANCE_PROP) or "").strip().lower()
        fact = (notion.select_value(p, FACT_PROP) or "").strip().lower()
        style = _style(notion.select_value(p, STYLE_PROP))
        if rel in ("yes", "no") and fact in ("ok", "not ok") and style:
            out[_norm(p["id"])] = {"relevance": rel, "fact_safety": fact, "style": style}
    return out


def _channel(channel: str | None, text: str) -> tuple[str, str]:
    """Канал і мова драфта. Кнопка без каналу (до 23.09) каналу не записала — тоді за мовою тексту."""
    if channel in pipeline.CHANNEL_LANG:
        return channel, pipeline.CHANNEL_LANG[channel]
    return ("fb", "uk") if re.search(r"[а-яіїєґ]", text, re.I) else ("x", "en")


async def collect() -> dict[str, int]:
    """Зібрати пари з Notion і postgen у hc_human. Нові рядки — з текстом статті з comms; уже зібрані — лише оновити оцінки
    людей (їх могли виправити). Нічого не коштує."""
    init_db()
    filt = {"and": [{"property": p, "select": {"is_not_empty": True}} for p in (RELEVANCE_PROP, FACT_PROP, STYLE_PROP)]}
    labels = human_labels(await notion.query_rows(os.environ["NOTION_DATABASE_ID"], filt))
    with pg._connect() as c:
        drafts = c.execute(
            """SELECT DISTINCT ON (replace(d.notion_page_id, '-', '')) replace(d.notion_page_id, '-', ''), d.article_id,
                      d.article_url, d.article_title, d.text, d.extraction, d.model_version, u.channel
               FROM drafts d LEFT JOIN used_articles u USING (article_id)
               WHERE replace(d.notion_page_id, '-', '') = ANY(%s)
               ORDER BY replace(d.notion_page_id, '-', ''), d.created_at""", (sorted(labels),)).fetchall()
        known = {r[0] for r in c.execute("SELECT notion_page_id FROM hc_human")}
        for page, h in labels.items():
            if page in known:
                c.execute("UPDATE hc_human SET relevance = %s, fact_safety = %s, style = %s WHERE notion_page_id = %s",
                          (h["relevance"], h["fact_safety"], h["style"], page))
    fresh = [d for d in drafts if d[0] not in known]
    texts = comms.fetch_by_ids(sorted({d[1] for d in fresh})) if fresh else {}
    added = 0
    with pg._connect() as c:
        for page, aid, url, title, text, extraction, model_version, channel in fresh:
            article = texts.get(aid)
            if not article or not (text or "").strip():
                continue
            ch, lang = _channel(channel, text)
            outcome = "needs_manual_review" if (extraction or {}).get("status") == "needs_manual_review" else "ok"
            h = labels[page]
            c.execute(
                """INSERT INTO hc_human (notion_page_id, article_id, channel, lang, url, title, article_text, source,
                                         draft_text, draft_outcome, extraction, model_version, relevance, fact_safety, style)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)""",
                (page, aid, ch, lang, url, title, article.text, article.source, text, outcome,
                 json.dumps(extraction, ensure_ascii=False) if extraction else None, model_version, h["relevance"],
                 h["fact_safety"], h["style"]))
            added += 1
    stats = {"rated_in_notion": len(labels), "with_draft": len(drafts), "already_collected": len(known & set(labels)),
             "added": added, "without_article_text": len(fresh) - added}
    log.info("звірка: зібрано пари %s", stats)
    return stats


# ---------- оцінка суддею ----------

def _case(r: tuple) -> hc_eval.EvalCase:
    page, aid, ch, lang, url, title, article_text, source, draft_text, outcome, extraction, model_version = r
    return hc_eval.EvalCase(run_id=RUN_ID, article_id=aid, channel=ch, lang=lang, model_version=model_version,
                            draft_outcome=outcome, draft_text=draft_text, title=title, url=url, source=source,
                            article_text=article_text, extraction=extraction or {})


def pending(judge_key: str, *, limit: int | None = None) -> list[tuple[str, hc_eval.EvalCase]]:
    """Пари, яких цей суддя ще не оцінив (або оцінив зі збоєм)."""
    with pg._connect() as c:
        rows = c.execute(
            """SELECT h.notion_page_id, h.article_id, h.channel, h.lang, h.url, h.title, h.article_text, h.source,
                      h.draft_text, h.draft_outcome, h.extraction, h.model_version
               FROM hc_human h LEFT JOIN hc_human_evals e
                 ON e.notion_page_id = h.notion_page_id AND e.judge_key = %s AND e.outcome <> 'ERROR'
               WHERE e.notion_page_id IS NULL ORDER BY h.notion_page_id LIMIT %s""", (judge_key, limit)).fetchall()
    return [(r[0], _case(r)) for r in rows]


def save_eval(page: str, judge_key: str, case: hc_eval.EvalCase, *, v: hc_eval.Verdict | None = None, raw: str | None = None,
              error: str | None = None, cost: float | None = None) -> None:
    with pg._connect() as c:
        c.execute(
            """INSERT INTO hc_human_evals (notion_page_id, judge_key, draft_sha, outcome, fact_safety, tier_a, tier_b, style,
                                           failure_types, problems, raw, error, cost_usd)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
               ON CONFLICT (notion_page_id, judge_key) DO UPDATE SET draft_sha = excluded.draft_sha,
                 outcome = excluded.outcome, fact_safety = excluded.fact_safety, tier_a = excluded.tier_a,
                 tier_b = excluded.tier_b, style = excluded.style, failure_types = excluded.failure_types,
                 problems = excluded.problems, raw = excluded.raw, error = excluded.error, cost_usd = excluded.cost_usd,
                 created_at = now()""",
            (page, judge_key, hc_eval.draft_sha(case.draft_text), v.outcome if v else "ERROR", v.fact_safety if v else None,
             v.tier_a if v else None, v.tier_b if v else None, v.style if v else None,
             json.dumps(v.failure_types if v else [], ensure_ascii=False),
             json.dumps(v.problems if v else [], ensure_ascii=False), raw, error, cost))


async def evaluate(judge: hc_eval.Judge | None = None, *, limit: int | None = None) -> dict[str, Any]:
    """Оцінити суддею пари, яких він ще не бачив. Кожна оцінка пишеться одразу; мертвий ключ чи кошти зупиняють усе."""
    judge = judge or hc_eval.get_judge()
    model = judge.key.split("/")[0]
    todo = pending(judge.key, limit=limit)
    loop = asyncio.get_running_loop()
    sem, abort = asyncio.Semaphore(hc_eval.PARALLEL), asyncio.Event()

    async def one(page: str, case: hc_eval.EvalCase) -> str:
        async with sem:
            if abort.is_set():
                return "ABORTED"
            reply = hc_eval.JudgeReply("")
            try:
                reply = await loop.run_in_executor(_pool, judge.evaluate, case)
                v = hc_eval.parse_verdict(reply.text)
            except hc_eval.JudgeFatal:
                abort.set()
                raise
            except Exception as e:
                log.warning("звірка %s: %s: %s", page, type(e).__name__, e)
                save_eval(page, judge.key, case, raw=reply.text or None, error=f"{type(e).__name__}: {e}"[:500],
                          cost=hc_eval.cost_usd(model, reply))
                return "ERROR"
            save_eval(page, judge.key, case, v=v, raw=reply.text, cost=hc_eval.cost_usd(model, reply))
            return v.outcome

    results: list[str | BaseException] = []
    for batch in (todo[:1], todo[1:]):  # перший запит сам: пише кеш промпта, решта його читає
        results += await asyncio.gather(*(one(p, c) for p, c in batch), return_exceptions=True)
    if fatal := next((r for r in results if isinstance(r, BaseException)), None):
        raise fatal
    summary = {"judge": judge.key, "cases": len(todo), "outcomes": dict(Counter(str(r) for r in results))}
    log.info("звірка: оцінено %s", summary)
    return summary


# ---------- метрики ----------

@dataclass
class Pair:
    """Один драфт: що сказала людина і що — суддя (None — суддя не дав відповіді на це поле)."""
    page: str
    channel: str
    title: str
    url: str
    human_relevance: str
    human_fact: str
    human_style: int
    judge_outcome: str | None = None
    judge_fact: str | None = None
    judge_style: int | None = None

    @property
    def human_publishable(self) -> bool:
        return self.human_relevance == "yes" and self.human_fact == "ok" and self.human_style >= 4

    @property
    def judge_publishable(self) -> bool | None:
        if self.judge_fact is None or self.judge_style is None:
            return None
        return self.judge_fact == "ok" and self.judge_style >= 4


def _ratio(a: int, b: int) -> float | None:
    return a / b if b else None


def kappa(matrix: dict[str, dict[str, int]]) -> float | None:
    """Kappa Коена для двох оцінювачів з двома класами: 1 — повний збіг, 0 — не краще за випадок."""
    n = sum(sum(row.values()) for row in matrix.values())
    if not n:
        return None
    observed = sum(matrix[k][k] for k in matrix) / n
    expected = sum(sum(matrix[k].values()) / n * sum(matrix[h][k] for h in matrix) / n for k in matrix)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def _fact(pairs: list[Pair]) -> dict[str, Any]:
    decided = [p for p in pairs if p.judge_fact in ("ok", "not ok")]
    matrix = {h: {j: sum(1 for p in decided if p.human_fact == h and p.judge_fact == j) for j in ("ok", "not ok")}
              for h in ("ok", "not ok")}
    agree = matrix["ok"]["ok"] + matrix["not ok"]["not ok"]
    return {"n": len(pairs), "decided": len(decided), "coverage": _ratio(len(decided), len(pairs)),
            "agreement": _ratio(agree, len(decided)), "matrix": matrix, "kappa": kappa(matrix),
            # суддя, що завжди каже not ok, теж «збігається» з усіма not ok людей — з цим і порівнюємо
            "always_not_ok": _ratio(sum(1 for p in decided if p.human_fact == "not ok"), len(decided)),
            "judge_too_lenient": matrix["not ok"]["ok"], "judge_too_strict": matrix["ok"]["not ok"]}


def _publishable(pairs: list[Pair]) -> dict[str, Any]:
    both = [p for p in pairs if p.judge_publishable is not None]
    tp = sum(1 for p in both if p.judge_publishable and p.human_publishable)
    judge_yes = sum(1 for p in both if p.judge_publishable)
    human_yes = sum(1 for p in both if p.human_publishable)
    return {"pairs": len(both), "agreement": _ratio(sum(1 for p in both if p.judge_publishable == p.human_publishable),
                                                    len(both)),
            "precision": _ratio(tp, judge_yes), "recall": _ratio(tp, human_yes),
            "human_rate": _ratio(human_yes, len(both)), "judge_rate": _ratio(judge_yes, len(both))}


def _style_stats(pairs: list[Pair]) -> dict[str, Any]:
    both = [p for p in pairs if p.judge_style is not None]
    return {"pairs": len(both), "exact": _ratio(sum(1 for p in both if p.judge_style == p.human_style), len(both)),
            "within_1": _ratio(sum(1 for p in both if abs((p.judge_style or 0) - p.human_style) <= 1), len(both)),
            "human_avg": sum(p.human_style for p in both) / len(both) if both else None,
            "judge_avg": sum(p.judge_style or 0 for p in both) / len(both) if both else None}


def metrics(pairs: list[Pair], target_: float | None = None) -> dict[str, Any]:
    """Усі метрики звірки; чиста функція."""
    goal = target() if target_ is None else target_
    out: dict[str, Any] = {"target": goal, "fact": _fact(pairs), "publishable": _publishable(pairs),
                           "style": _style_stats(pairs),
                           "judge_outcomes": dict(Counter(p.judge_outcome or "не оцінено" for p in pairs))}
    out["channels"] = {ch: {"fact": _fact(sub), "publishable": _publishable(sub)}
                       for ch in ("fb", "x") if (sub := [p for p in pairs if p.channel == ch])}
    agreement = out["fact"]["agreement"]
    out["passed"] = agreement is not None and agreement >= goal
    return out


def disagreements(pairs: list[Pair]) -> list[Pair]:
    """Драфти, де суддя розійшовся з людиною по fact safety: спершу ті, де він пропустив not ok (небезпечніше)."""
    wrong = [p for p in pairs if p.judge_fact in ("ok", "not ok") and p.judge_fact != p.human_fact]
    return sorted(wrong, key=lambda p: (p.judge_fact != "ok", p.channel, p.page))


def load_pairs(judge_key: str) -> list[Pair]:
    with pg._connect() as c:
        rows = c.execute(
            """SELECT h.notion_page_id, h.channel, h.title, h.url, h.relevance, h.fact_safety, h.style, e.outcome,
                      e.fact_safety, e.style
               FROM hc_human h LEFT JOIN hc_human_evals e ON e.notion_page_id = h.notion_page_id AND e.judge_key = %s
               ORDER BY h.channel, h.notion_page_id""", (judge_key,)).fetchall()
    return [Pair(page=r[0], channel=r[1], title=r[2], url=r[3], human_relevance=r[4], human_fact=r[5], human_style=r[6],
                 judge_outcome=r[7], judge_fact=r[8], judge_style=r[9]) for r in rows]


def cost(judge_key: str) -> float:
    with pg._connect() as c:
        r = c.execute("SELECT coalesce(sum(cost_usd), 0) FROM hc_human_evals WHERE judge_key = %s", (judge_key,)).fetchone()
    return float(r[0]) if r else 0.0


# ---------- звіт ----------

def _matrix_table(m: dict[str, dict[str, int]]) -> list[str]:
    return hc_report.table(["Людина \\ суддя", "ok", "not ok"],
                           [[h, str(m[h]["ok"]), str(m[h]["not ok"])] for h in ("ok", "not ok")])


def render(m: dict[str, Any], judge_key: str, *, pairs: list[Pair] | None = None, spent: float | None = None,
           when: datetime | None = None) -> str:
    f, pub, st = m["fact"], m["publishable"], m["style"]
    day = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")
    icon, verdict = ("✅", "суддю можна використовувати") if m["passed"] else ("❌", "суддя не досягає цілі")
    out = [f"# Health Check · звірка судді з людьми · {day}", "",
           f"> {icon} **Збіг по fact safety {pct(f['agreement'])} (ціль ≥ {pct(m['target'])}): {verdict}**", "",
           f"- Драфтів, оцінених людьми повністю: {f['n']}; суддя дав fact safety на {f['decided']} ({pct(f['coverage'])})",
           f"- Базова лінія «завжди not ok»: {pct(f['always_not_ok'])} — суддя має бути помітно кращим за неї",
           f"- Kappa Коена: {num(f['kappa'])} (0 — не краще за випадок, 1 — повний збіг)",
           f"- Суддя пропустив not ok (сказав ok): {f['judge_too_lenient']}; надто суворий (сказав not ok на ok): "
           f"{f['judge_too_strict']}",
           f"- Суддя: `{judge_key}`" + (f"; вартість оцінки ${spent:.2f}" if spent is not None else ""), "",
           "## Fact safety: матриця помилок", "", *_matrix_table(f["matrix"]),
           "## «Можна публікувати» (fact ok + Style 4–5)", "",
           f"- Збіг: {pct(pub['agreement'])} на {pub['pairs']} драфтах",
           f"- Precision: {pct(pub['precision'])} — з того, що суддя пропустив би в публікацію, люди теж пропустили б",
           f"- Recall: {pct(pub['recall'])} — з того, що люди визнали публікабельним, суддя теж визнав",
           f"- Частка «можна публікувати»: у людей {pct(pub['human_rate'])}, у судді {pct(pub['judge_rate'])}", "",
           "## Style", "",
           f"- Точний збіг: {pct(st['exact'])}, ±1 бал: {pct(st['within_1'])} на {st['pairs']} драфтах",
           f"- Середній Style: у людей {num(st['human_avg'])}, у судді {num(st['judge_avg'])}", "",
           "## За каналами", ""]
    out += hc_report.table(
        ["Канал", "Драфтів", "Суддя дав вердикт", "Збіг fact safety", "Kappa", "Збіг «можна публікувати»"],
        [[CHANNEL[ch], str(c["fact"]["n"]), str(c["fact"]["decided"]), pct(c["fact"]["agreement"]), num(c["fact"]["kappa"]),
          pct(c["publishable"]["agreement"])] for ch, c in m["channels"].items()])
    out += ["## Відповіді судді", "", *[f"- {k}: {v}" for k, v in sorted(m["judge_outcomes"].items())], ""]
    wrong = disagreements(pairs or [])
    if wrong:
        out += ["## Де суддя розійшовся з людьми (fact safety)", ""]
        out += [f"- {CHANNEL[p.channel]} · [{p.title or p.page}]({p.url}) — людина: {p.human_fact}, суддя: {p.judge_fact}"
                for p in wrong[:SHOWN_DISAGREEMENTS]]
        if len(wrong) > SHOWN_DISAGREEMENTS:
            out.append(f"- … і ще {len(wrong) - SHOWN_DISAGREEMENTS}")
        out.append("")
    out += ["## Як рахували", "",
            "- Люди: рядки таблиці MVP, де заповнені Article relevance, Fact safety і Style score. Суддя бачив той самий "
            "драфт і повний текст статті, оцінював за інструкцією Антона в режимі dry (у Notion нічого не пише).",
            "- Збіг по fact safety рахуємо лише там, де суддя дав вердикт; скільки таких — у «покритті».",
            "- У людей «можна публікувати» ще й вимагає Article relevance = yes; суддя крок relevance пропускає (див. §17).", ""]
    return "\n".join(out).rstrip() + "\n"


def save(judge_key: str, m: dict[str, Any], markdown: str, notion_url: str | None) -> None:
    with pg._connect() as c:
        c.execute("INSERT INTO hc_calibrations (judge_key, agreement, target, decided, metrics, markdown, notion_url) "
                  "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)",
                  (judge_key, m["fact"]["agreement"], m["target"], m["fact"]["decided"],
                   json.dumps(m, ensure_ascii=False), markdown, notion_url))


def latest(judge_key: str) -> dict[str, Any] | None:
    """Остання звірка цього судді: agreement, target, decided, created_at (+ notion_url, metrics, markdown). Таблиці ще
    нема чи бази нема — None: звіт прогону тоді просто скаже, що суддю не звірено."""
    try:
        with pg._connect() as c:
            r = c.execute("SELECT agreement, target, decided, created_at, notion_url, metrics, markdown FROM hc_calibrations "
                          "WHERE judge_key = %s AND agreement IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                          (judge_key,)).fetchone()
    except (psycopg.errors.UndefinedTable, KeyError):
        return None
    keys = ("agreement", "target", "decided", "created_at", "notion_url", "metrics", "markdown")
    return dict(zip(keys, r, strict=True)) if r else None


async def publish(markdown: str) -> str | None:
    """Підсторінка під NOTION_HEALTHCHECK_PAGE_ID (поряд із таблицею прогонів). Не задано — None. Лише створює."""
    parent = os.environ.get("NOTION_HEALTHCHECK_PAGE_ID", "").replace("-", "")
    if not parent:
        return None
    title, _, body = markdown.partition("\n")
    page = await notion.create_page(parent, title.removeprefix("# "), hc_report.to_notion_blocks(body))
    return page.get("url")


def alert_text(m: dict[str, Any], judge_key: str, url: str | None) -> str:
    mention = os.environ.get("HEALTHCHECK_CALIBRATION_MENTION", "").strip()
    f = m["fact"]
    lines = [f"❌ Health Check: суддя збігається з людьми по fact safety лише на {pct(f['agreement'])} "
             f"(ціль ≥ {pct(m['target'])}, {f['decided']} драфтів, «завжди not ok» дало б {pct(f['always_not_ok'])})",
             f"Пропустив not ok: {f['judge_too_lenient']} · надто суворий: {f['judge_too_strict']} · kappa {num(f['kappa'])}",
             "Поки цього не виправлено (інструкція чи модель судді), вердиктам Health Check варто довіряти обережно.",
             f"Суддя: {judge_key}",
             f"Звіт: {url}" if url else "Звіт: GET /health-check/calibration (з сервера)"]
    return (f"{mention} " if mention else "") + "\n".join(lines)


async def report(judge_key: str | None = None, *, publish_: bool = True, alert: bool = True) -> dict[str, Any]:
    """Метрики й звіт з уже наявних оцінок (нічого не оцінює): зберегти, опублікувати, повідомити, якщо нижче цілі."""
    key = judge_key or hc_eval.current_judge_key()
    pairs = load_pairs(key)
    m = metrics(pairs)
    markdown = render(m, key, pairs=pairs, spent=cost(key))
    url = None
    if publish_:
        try:
            url = await publish(markdown)
        except Exception:
            log.exception("звірка: звіт не додано в Notion")
    save(key, m, markdown, url)
    if alert and m["fact"]["agreement"] is not None and not m["passed"]:
        try:
            await notify.send(alert_text(m, key, url), verdict="calibration")
        except Exception:
            log.exception("звірка: повідомлення не надіслано")
    log.info("звірка %s: збіг %s на %s (ціль %s)", key, pct(m["fact"]["agreement"]), m["fact"]["decided"], pct(m["target"]))
    return {"judge": key, "agreement": m["fact"]["agreement"], "target": m["target"], "decided": m["fact"]["decided"],
            "passed": m["passed"], "notion_url": url, "metrics": m, "markdown": markdown}


def is_busy() -> bool:
    return _busy


async def run(judge: hc_eval.Judge | None = None, *, limit: int | None = None, collect_first: bool = True
              ) -> dict[str, Any] | None:
    """Зібрати → оцінити → звіт. Для фонового запуску з ендпоінта: не кидає винятків, збій іде повідомленням."""
    global _busy
    if _busy:
        return None
    _busy = True
    try:
        if collect_first:
            await collect()
        judge = judge or hc_eval.get_judge()
        await evaluate(judge, limit=limit)
        return await report(judge.key)
    except Exception as e:
        log.exception("звірка судді не завершилась")
        try:
            await notify.send(f"🔴 Health Check: звірка судді з людьми не завершилась\n{type(e).__name__}: {e}"[:600],
                              verdict="problem")
        except Exception:
            log.exception("звірка: повідомлення про збій не надіслано")
        return None
    finally:
        _busy = False


if __name__ == "__main__":
    from dotenv import load_dotenv

    from . import logsafe

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logsafe.install()
    usage = "usage: python -m app.hc_calibrate [run [--limit N]] | collect | report"
    args = sys.argv[1:] or ["run"]
    if args[0] == "collect":
        print(json.dumps(asyncio.run(collect()), ensure_ascii=False, indent=1))
    elif args[0] == "report":
        init_db()
        print(asyncio.run(report())["markdown"])
    elif args[0] == "run":
        lim = int(args[args.index("--limit") + 1]) if "--limit" in args else None
        result = asyncio.run(run(limit=lim))
        if result is None:
            sys.exit("звірка не завершилась — див. журнал")
        print(result["markdown"])
    else:
        sys.exit(usage)
