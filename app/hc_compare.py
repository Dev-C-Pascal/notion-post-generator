"""Health Check, пункт 4: порівняння прогонів. Одні й ті самі 100 статей, тож порівнюємо попарно: що сталося з кожною
статтею між базовим і новим прогоном, а не лише середні. Це відсікає шум вибірки: відсоток зміг впасти на 5 п. п. лише
тому, що п'ять драфтів по-іншому розійшлися, а знаковий тест на розбіжних парах каже, чи це більше за випадок.

Рахуємо те саме, що Антон: Fact safety = ok, частка Style 4–5 (ціль критеріїв — 20%), Tier A / Tier B на драфт, плюс
дві речі, яких у людській оцінці не було: скільки статей модель взагалі закрила драфтом і чи самоперевірка моделі
(needs_manual_review) збігається з вердиктом судді.

Порівнювати можна лише оцінки одного судді (judge_key): інша модель чи інструкція — інша лінійка. Чистих функцій тут
більшість (summarize, compare, sign_test_p), вони без бази; з базою працюють лише load_rows, run_info та вибір бази.
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


SPECS = (
    Spec("drafted_rate", "Модель дала драфт", True, lambda r: float(r.drafted), True),
    Spec("fact_ok_rate", "Fact safety = ok", True, lambda r: _flag(None if r.fact_safety is None else r.fact_safety == "ok"),
         True),
    Spec("style_45_rate", "Style 4–5", True, lambda r: _flag(None if r.style is None else r.style >= 4), True),
    Spec("publishable_rate", "Fact ok і Style 4–5", True,
         lambda r: _flag(None if r.fact_safety is None or r.style is None else r.fact_safety == "ok" and r.style >= 4), True),
    Spec("style_avg", "Середній Style", True, lambda r: None if r.style is None else float(r.style), False),
    Spec("tier_a_avg", "Tier A на драфт", False, lambda r: None if r.tier_a is None else float(r.tier_a), False),
    Spec("tier_b_avg", "Tier B на драфт", False, lambda r: None if r.tier_b is None else float(r.tier_b), False),
)
SPEC = {s.key: s for s in SPECS}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def summarize(rows: list[Row]) -> dict[str, Any]:
    """Усі числа прогону (або каналу): лічильники, частки, типи помилок, якість самоперевірки моделі."""
    drafted = [r for r in rows if r.drafted]
    judged = [r for r in rows if r.judged]
    out: dict[str, Any] = {"n": len(rows), "drafted": len(drafted), "refused": sum(r.draft == "refused" for r in rows),
                           "errors": sum(r.draft in ("error", "missing") for r in rows),
                           "flagged": sum(r.draft == "needs_manual_review" for r in rows), "judged": len(judged),
                           "judge_errors": sum(r.eval == "ERROR" for r in rows),
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


def compare(base_run: str, base: list[Row], new_run: str, new: list[Row]) -> Comparison:
    deltas = {scope: _deltas([r for r in base if scope in ("all", r.channel)], [r for r in new if scope in ("all", r.channel)])
              for scope in SCOPES}
    reg, imp = _changes(base, new)
    return Comparison(base_run, new_run, deltas, summarize_scopes(base), summarize_scopes(new), reg, imp,
                      same_articles={r.article_id for r in base} == {r.article_id for r in new})


# ---------- дані з бази ----------

def load_rows(run_id: str, judge_key: str) -> list[Row]:
    """Усі еталонні статті з тим, що з ними сталося в прогоні; оцінка — лише цього судді."""
    with pg._connect() as c:
        rows = c.execute(
            """SELECT a.article_id, a.channel, a.title, a.url, coalesce(d.outcome, 'missing'), e.outcome, e.fact_safety,
                      e.tier_a, e.tier_b, e.style, e.status, e.source_captured, e.failure_types, coalesce(e.cost_usd, 0)
               FROM hc_articles a
               LEFT JOIN hc_drafts d ON d.article_id = a.article_id AND d.run_id = %(run)s
               LEFT JOIN hc_evals e ON e.article_id = a.article_id AND e.run_id = %(run)s AND e.judge_key = %(key)s
               ORDER BY a.channel, a.article_id""", {"run": run_id, "key": judge_key}).fetchall()
    return [Row(article_id=r[0], channel=r[1], title=r[2], url=r[3], draft=r[4], eval=r[5], fact_safety=r[6], tier_a=r[7],
                tier_b=r[8], style=r[9], status=r[10], source_captured=r[11], failure_types=r[12] or [], cost_usd=r[13])
            for r in rows]


def run_info(run_id: str) -> dict[str, Any] | None:
    with pg._connect() as c:
        r = c.execute("SELECT run_id, trigger, model_version, status, started_at, finished_at FROM hc_runs WHERE run_id = %s",
                      (run_id,)).fetchone()
    return dict(zip(("run_id", "trigger", "model_version", "status", "started_at", "finished_at"), r, strict=True)) if r else None


def pending_evals(run_id: str, judge_key: str) -> int:
    """Скільки драфтів прогону ще без успішної оцінки цього судді (0 — оцінку завершено)."""
    with pg._connect() as c:
        r = c.execute(
            """SELECT count(*) FROM hc_drafts d
               LEFT JOIN hc_evals e ON e.run_id = d.run_id AND e.article_id = d.article_id AND e.judge_key = %s
                                   AND e.outcome <> 'ERROR'
               WHERE d.run_id = %s AND d.outcome IN ('ok', 'needs_manual_review') AND coalesce(d.text, '') <> ''
                 AND e.article_id IS NULL""", (judge_key, run_id)).fetchone()
    return int(r[0]) if r else 0


def previous_run(run_id: str) -> str | None:
    """Найближчий за часом попередній завершений прогін — база «що змінилось з минулого разу»."""
    with pg._connect() as c:
        r = c.execute(
            """SELECT p.run_id FROM hc_runs p, hc_runs r WHERE r.run_id = %s AND p.status = 'done'
                 AND p.started_at < r.started_at ORDER BY p.started_at DESC LIMIT 1""", (run_id,)).fetchone()
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
