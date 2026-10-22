"""Health Check, пункт 4: порівняння прогонів. Одні й ті самі 100 статей, тож порівнюємо попарно: що сталося з кожною
статтею між базовим і новим прогоном, а не лише середні. Це відсікає шум вибірки: відсоток зміг впасти на 5 п. п. лише
тому, що п'ять драфтів по-іншому розійшлися, а знаковий тест на розбіжних парах каже, чи це більше за випадок.

Рахуємо те саме, що Антон: Fact safety = ok, частка Style 4–5 (ціль критеріїв — 20%), Tier A / Tier B на драфт, плюс
дві речі, яких у людській оцінці не було: скільки статей модель взагалі закрила драфтом і чи самоперевірка моделі
(needs_manual_review) збігається з вердиктом судді.

Порівнювати можна лише оцінки одного судді (judge_key): інша модель чи інструкція — інша лінійка. Чистих функцій тут
більшість (summarize, compare, sign_test_p), вони без бази; з базою працюють лише load_rows, run_info та вибір бази.

Головна метрика — publishable_rate, «можна публікувати» (Fact safety ok + Style 4–5), від УСІХ статей набору: стаття, на яку
модель не дала драфта (відмова, збій), — це «ні». Так модель, що стала частіше відмовлятись, не виглядає кращою лише тому,
що на решті статей відсоток не змінився.
"""
from __future__ import annotations

import dataclasses
import math
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import pg

DRAFTED = ("ok", "needs_manual_review")  # модель дала текст (другий — з не пройденою самоперевіркою)
SCOPES = ("all", "fb", "x")
STYLE_TARGET = 0.20  # ціль критеріїв: частка драфтів зі Style 4–5
MIN_COVERAGE = 0.8  # скільки драфтів має отримати оцінку судді, щоб порівнянню можна було довіряти


@dataclass
class Row:
    """Одна еталонна стаття в одному прогоні: що зробила модель і що сказав суддя."""
    article_id: str
    channel: str
    title: str = ""
    url: str = ""
    draft: str = "missing"  # ok | needs_manual_review | refused | error | missing (у прогоні немає рядка)
    eval: str | None = None  # outcome судді: EVALUATED | SECOND_OPINION | ERROR | None (не оцінювався)
    fact_safety: str | None = None
    tier_a: int | None = None
    tier_b: int | None = None
    style: int | None = None
    status: str | None = None
    source_captured: str | None = None
    failure_types: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    reused: bool = False  # оцінку взято з попереднього прогону: той самий текст драфта, нових викликів не було
    duration_ms: int | None = None  # скільки модель писала драфт (і коли відповіла збоєм — скільки чекали)

    @property
    def drafted(self) -> bool:
        return self.draft in DRAFTED

    @property
    def judged(self) -> bool:
        return self.eval in ("EVALUATED", "SECOND_OPINION")


# ---------- метрики ----------

@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    higher_is_better: bool
    value: Callable[[Row], float | None]  # число для однієї статті; None — метрика до неї не застосовна
    share: bool  # частка (0/1 на статтю) чи середнє


def _flag(cond: bool | None) -> float | None:
    return None if cond is None else float(cond)


def publishable(r: Row) -> bool | None:
    """«Можна публікувати» = Fact safety ok і Style 4–5 (правила рецензентів). Статті без драфта (відмова, збій моделі) —
    «ні»; драфт, який суддя ще не оцінив, — невідомо (None), у відсоток він не входить."""
    if not r.drafted:
        return False
    if r.fact_safety is None or r.style is None:
        return None
    return r.fact_safety == "ok" and r.style >= 4


SPECS = (
    Spec("drafted_rate", "Модель дала драфт", True, lambda r: float(r.drafted), True),
    Spec("fact_ok_rate", "Fact safety = ok", True, lambda r: _flag(None if r.fact_safety is None else r.fact_safety == "ok"),
         True),
    Spec("style_45_rate", "Style 4–5", True, lambda r: _flag(None if r.style is None else r.style >= 4), True),
    Spec("publishable_rate", "Можна публікувати (fact ok + Style 4–5)", True, lambda r: _flag(publishable(r)), True),
    Spec("style_avg", "Середній Style", True, lambda r: None if r.style is None else float(r.style), False),
    Spec("tier_a_avg", "Tier A на драфт", False, lambda r: None if r.tier_a is None else float(r.tier_a), False),
    Spec("tier_b_avg", "Tier B на драфт", False, lambda r: None if r.tier_b is None else float(r.tier_b), False),
)
SPEC = {s.key: s for s in SPECS}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _percentile(sorted_values: list[float], q: float) -> float | None:
    """Найближчий ранг: q = 0,5 — медіана, 0,9 — дев'яностий процентиль."""
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))]


def summarize(rows: list[Row]) -> dict[str, Any]:
    """Усі числа прогону (або каналу): лічильники, частки, типи помилок, якість самоперевірки моделі."""
    drafted = [r for r in rows if r.drafted]
    judged = [r for r in rows if r.judged]
    out: dict[str, Any] = {"n": len(rows), "drafted": len(drafted), "refused": sum(r.draft == "refused" for r in rows),
                           "errors": sum(r.draft in ("error", "missing") for r in rows),
                           "flagged": sum(r.draft == "needs_manual_review" for r in rows), "judged": len(judged),
                           "judge_errors": sum(r.eval == "ERROR" for r in rows), "reused": sum(r.reused for r in rows),
                           "second_opinion": sum(r.eval == "SECOND_OPINION" for r in rows),
                           "coverage": _ratio(len(judged), len(drafted)),  # частка драфтів, що отримали оцінку
                           "refusal_rate": _ratio(sum(r.draft == "refused" for r in rows), len(rows)),
                           "error_rate": _ratio(sum(r.draft in ("error", "missing") for r in rows), len(rows)),
                           "cost_usd": round(sum(r.cost_usd for r in rows), 4)}
    for s in SPECS:
        vals = [v for r in rows if (v := s.value(r)) is not None]
        out[s.key] = _mean(vals)
        out[s.key + "_n"] = len(vals)
    # самоперевірка моделі проти судді: чи справді там фейк, де модель підняла прапорець, і чи піднімає вона його там, де фейк є
    flagged = [r for r in drafted if r.draft == "needs_manual_review" and r.fact_safety is not None]
    fake = [r for r in drafted if r.fact_safety == "not ok"]
    out["flag_precision"] = _ratio(sum(r.fact_safety == "not ok" for r in flagged), len(flagged))
    out["flag_recall"] = _ratio(sum(r.draft == "needs_manual_review" for r in fake), len(fake))
    seconds = sorted(r.duration_ms / 1000 for r in rows if r.duration_ms is not None)
    out["gen_median_s"], out["gen_p90_s"], out["gen_max_s"] = _percentile(seconds, 0.5), _percentile(seconds, 0.9), \
        (seconds[-1] if seconds else None)
    out["failure_types"] = dict(Counter(t for r in judged for t in r.failure_types).most_common())
    out["status"] = dict(Counter(r.status for r in judged if r.status).most_common())
    out["source_captured"] = dict(Counter(r.source_captured for r in judged if r.source_captured))
    return out


def summarize_scopes(rows: list[Row]) -> dict[str, dict[str, Any]]:
    return {"all": summarize(rows), "fb": summarize([r for r in rows if r.channel == "fb"]),
            "x": summarize([r for r in rows if r.channel == "x"])}


# ---------- порівняння ----------

def sign_test_p(worse: int, better: int) -> float | None:
    """Двосторонній точний знаковий тест на розбіжних парах: імовірність побачити таку перевагу в один бік, якщо
    насправді зміни випадкові (50/50). None — розбіжних пар немає, порівнювати нема чого."""
    n = worse + better
    if n == 0:
        return None
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(min(worse, better) + 1)) / 2 ** n)


@dataclass
class MetricDelta:
    key: str
    label: str
    higher_is_better: bool
    share: bool
    base: float | None
    new: float | None
    pairs: int  # статей, де метрика визначена в обох прогонах
    worse: int  # з них стало гірше
    better: int  # стало краще
    p: float | None  # знаковий тест по worse/better

    @property
    def delta(self) -> float | None:
        return None if self.base is None or self.new is None else self.new - self.base

    @property
    def worsening(self) -> float | None:
        """Наскільки стало гірше в одиницях метрики (частка — у частках, не в п. п.); від'ємне — покращення."""
        d = self.delta
        return None if d is None else (-d if self.higher_is_better else d)

    def as_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "delta": self.delta, "worsening": self.worsening}


@dataclass
class Comparison:
    base_run: str
    new_run: str
    deltas: dict[str, list[MetricDelta]]  # за областями: all / fb / x
    base: dict[str, dict[str, Any]]  # summarize_scopes кожного з прогонів
    new: dict[str, dict[str, Any]]
    regressions: list[dict[str, Any]]
    improvements: list[dict[str, Any]]
    same_articles: bool  # обидва прогони охоплюють однакові статті
    warnings: list[str] = field(default_factory=list)  # чому порівнянню не варто довіряти (неповна оцінка тощо)
    meta: dict[str, Any] = field(default_factory=dict)  # run_info обох прогонів: модель, час, статус
    lost: list[dict[str, Any]] = field(default_factory=list)  # були «можна публікувати», стали «ні»
    gained: list[dict[str, Any]] = field(default_factory=list)  # були «ні», стали «можна публікувати»

    def delta(self, key: str, scope: str = "all") -> MetricDelta:
        return next(d for d in self.deltas[scope] if d.key == key)

    def as_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out["deltas"] = {scope: [d.as_dict() for d in ds] for scope, ds in self.deltas.items()}
        return out


def _deltas(base: list[Row], new: list[Row]) -> list[MetricDelta]:
    b_by, n_by = {r.article_id: r for r in base}, {r.article_id: r for r in new}
    out = []
    for s in SPECS:
        b_vals = [v for r in base if (v := s.value(r)) is not None]
        n_vals = [v for r in new if (v := s.value(r)) is not None]
        worse = better = pairs = 0
        for aid in b_by.keys() & n_by.keys():
            bv, nv = s.value(b_by[aid]), s.value(n_by[aid])
            if bv is None or nv is None:
                continue
            pairs += 1
            if nv != bv:
                improved = (nv > bv) == s.higher_is_better
                better += improved
                worse += not improved
        out.append(MetricDelta(s.key, s.label, s.higher_is_better, s.share, _mean(b_vals), _mean(n_vals), pairs, worse, better,
                               sign_test_p(worse, better)))
    return out


def _brief(r: Row) -> str:
    if not r.drafted:
        return {"refused": "відмова моделі", "error": "збій моделі", "missing": "немає в прогоні"}.get(r.draft, r.draft)
    return f"fact {r.fact_safety or '—'} · Style {r.style if r.style is not None else '—'}"


def _changes(base: list[Row], new: list[Row]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Статті, де між прогонами сталася помітна зміна: фейк з'явився / зник, Style ±2 і більше, драфт зник / з'явився."""
    b_by = {r.article_id: r for r in base}
    reg: list[dict[str, Any]] = []
    imp: list[dict[str, Any]] = []
    for n in sorted(new, key=lambda r: (r.channel, r.article_id)):
        b = b_by.get(n.article_id)
        if b is None:
            continue
        bad: list[str] = []
        good: list[str] = []
        if b.drafted != n.drafted:
            (good if n.drafted else bad).append("драфт з'явився" if n.drafted else "драфта більше немає")
        if b.fact_safety and n.fact_safety and b.fact_safety != n.fact_safety:
            (good if n.fact_safety == "ok" else bad).append("фейк зник" if n.fact_safety == "ok" else "з'явився фейк")
        if b.style is not None and n.style is not None and abs(n.style - b.style) >= 2:
            (good if n.style > b.style else bad).append(f"Style {b.style} → {n.style}")
        for bucket, whats in ((reg, bad), (imp, good)):
            if whats:
                bucket.append({"article_id": n.article_id, "channel": n.channel, "title": n.title, "url": n.url,
                               "what": "; ".join(whats), "before": _brief(b), "after": _brief(n)})
    return reg, imp


def _why_not(r: Row) -> str:
    """Чому стаття не «можна публікувати» в цьому прогоні."""
    if not r.drafted:
        return _brief(r)
    if r.fact_safety != "ok":
        types = f" ({', '.join(r.failure_types[:3])})" if r.failure_types else ""
        return f"Fact safety = not ok{types}"
    return f"Style {r.style}"


def _lost_and_gained(base: list[Row], new: list[Row]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Статті, що перейшли межу «можна публікувати» між прогонами. Невідомі (драфт без оцінки) не рахуємо ні туди, ні сюди."""
    b_by = {r.article_id: r for r in base}
    lost: list[dict[str, Any]] = []
    gained: list[dict[str, Any]] = []
    for n in sorted(new, key=lambda r: (r.channel, r.article_id)):
        b = b_by.get(n.article_id)
        if b is None:
            continue
        before, after = publishable(b), publishable(n)
        if before is True and after is False:
            bucket, what = lost, _why_not(n)
        elif before is False and after is True:
            bucket, what = gained, f"Fact safety ok, Style {n.style}"
        else:
            continue
        bucket.append({"article_id": n.article_id, "channel": n.channel, "title": n.title, "url": n.url, "what": what,
                       "before": _brief(b), "after": _brief(n)})
    return lost, gained


def compare(base_run: str, base: list[Row], new_run: str, new: list[Row]) -> Comparison:
    deltas = {scope: _deltas([r for r in base if scope in ("all", r.channel)], [r for r in new if scope in ("all", r.channel)])
              for scope in SCOPES}
    reg, imp = _changes(base, new)
    lost, gained = _lost_and_gained(base, new)
    return Comparison(base_run, new_run, deltas, summarize_scopes(base), summarize_scopes(new), reg, imp,
                      same_articles={r.article_id for r in base} == {r.article_id for r in new}, lost=lost, gained=gained)


# ---------- дані з бази ----------

def load_rows(run_id: str, judge_key: str) -> list[Row]:
    """Усі еталонні статті з тим, що з ними сталося в прогоні; оцінка — лише цього судді."""
    with pg._connect() as c:
        rows = c.execute(
            """SELECT a.article_id, a.channel, a.title, a.url, coalesce(d.outcome, 'missing'), e.outcome, e.fact_safety,
                      e.tier_a, e.tier_b, e.style, e.status, e.source_captured, e.failure_types, coalesce(e.cost_usd, 0),
                      e.reused_from IS NOT NULL, d.duration_ms
               FROM hc_articles a
               LEFT JOIN hc_drafts d ON d.article_id = a.article_id AND d.run_id = %(run)s
               LEFT JOIN hc_evals e ON e.article_id = a.article_id AND e.run_id = %(run)s AND e.judge_key = %(key)s
               ORDER BY a.channel, a.article_id""", {"run": run_id, "key": judge_key}).fetchall()
    return [Row(article_id=r[0], channel=r[1], title=r[2], url=r[3], draft=r[4], eval=r[5], fact_safety=r[6], tier_a=r[7],
                tier_b=r[8], style=r[9], status=r[10], source_captured=r[11], failure_types=r[12] or [], cost_usd=r[13],
                reused=bool(r[14]), duration_ms=r[15])
            for r in rows]


def run_info(run_id: str) -> dict[str, Any] | None:
    with pg._connect() as c:
        r = c.execute("SELECT run_id, trigger, model_version, status, started_at, finished_at, note FROM hc_runs "
                      "WHERE run_id = %s", (run_id,)).fetchone()
    keys = ("run_id", "trigger", "model_version", "status", "started_at", "finished_at", "note")
    return dict(zip(keys, r, strict=True)) if r else None


def pending_evals(run_id: str, judge_key: str, *, retry_errors: bool = True) -> int:
    """Скільки драфтів прогону ще без оцінки цього судді. retry_errors: чи вважати недоробленими ті, де суддя збоїв
    (ERROR) — для порівняння так, а для автоматичного супроводу ні: інакше суддя, що стабільно не віддає розбірливої
    відповіді, змушував би щогодини платити за ті самі виклики."""
    with pg._connect() as c:
        r = c.execute(
            """SELECT count(*) FROM hc_drafts d
               LEFT JOIN hc_evals e ON e.run_id = d.run_id AND e.article_id = d.article_id AND e.judge_key = %(key)s
                                   AND (NOT %(retry)s OR e.outcome <> 'ERROR')
               WHERE d.run_id = %(run)s AND d.outcome IN ('ok', 'needs_manual_review') AND coalesce(d.text, '') <> ''
                 AND e.article_id IS NULL""", {"key": judge_key, "run": run_id, "retry": retry_errors}).fetchone()
    return int(r[0]) if r else 0


def latest_run(status: str = "done") -> str | None:
    with pg._connect() as c:
        r = c.execute("SELECT run_id FROM hc_runs WHERE status = %s ORDER BY started_at DESC LIMIT 1", (status,)).fetchone()
    return r[0] if r else None


def previous_run(run_id: str) -> str | None:
    """Найближчий за часом попередній завершений прогін — база «що змінилось з минулого разу»."""
    with pg._connect() as c:
        r = c.execute(
            """SELECT p.run_id FROM hc_runs p, hc_runs r WHERE r.run_id = %s AND p.status = 'done'
                 AND p.started_at < r.started_at ORDER BY p.started_at DESC LIMIT 1""", (run_id,)).fetchone()
    return r[0] if r else None


def last_successful_run(run_id: str, judge_key: str | None = None) -> str | None:
    """Остання успішна база вердикту: найближчий за часом ПОПЕРЕДНІЙ завершений прогін, що отримав вердикт «пройшла» або став
    базою (перший). Прогони, що не пройшли чи без вердикту, базою не стають: модель, яка провалилась, не знижує планку.
    Вердикт того судді, що оцінює зараз, береться першим; якщо суддю змінили — найближчий прогін з будь-яким вердиктом."""
    with pg._connect() as c:
        r = c.execute(
            """SELECT p.run_id FROM hc_runs p
               JOIN hc_reports v ON v.run_id = p.run_id AND v.verdict IN ('passed', 'baseline')
               JOIN hc_runs cur ON cur.run_id = %(run)s
               WHERE p.status = 'done' AND p.started_at < cur.started_at
               ORDER BY p.started_at DESC, (v.judge_key = %(key)s) DESC LIMIT 1""",
            {"run": run_id, "key": judge_key}).fetchone()
    return r[0] if r else None


def baseline_run(run_id: str) -> str | None:
    """Еталонний прогін «прийнятої» версії: HEALTHCHECK_BASELINE_RUN, інакше найстаріший завершений. Для самого себе — None."""
    pinned = os.environ.get("HEALTHCHECK_BASELINE_RUN")
    with pg._connect() as c:
        if pinned:
            found = c.execute("SELECT run_id FROM hc_runs WHERE run_id = %s AND status = 'done'", (pinned,)).fetchone()
            base = found[0] if found else None
        else:
            first = c.execute("SELECT run_id FROM hc_runs WHERE status = 'done' ORDER BY started_at LIMIT 1").fetchone()
            base = first[0] if first else None
    return None if base == run_id else base
