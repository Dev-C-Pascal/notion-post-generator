"""Health Check, пункт 5: вердикт «пройшла / не пройшла». Агент лише читає драфти й виписує помилки; вердикт ставить цей код
за правилами команди (Notion: «Перевіряти, чи не впала якість постів»):

  • міряємо «можна публікувати» (Fact safety ok + Style 4–5) від усіх статей набору;
  • порівнюємо з ОСТАННІМ УСПІШНИМ прогоном на тих самих статтях: впало більше за поріг — ❌ модель не пройшла, інакше ✅;
  • поріг — 5 п. п. (HEALTHCHECK_THRESHOLD_PP), але не менший за власний «шум» моделі (HEALTHCHECK_NOISE_PP): модель щоразу
    пише трохи по-різному, і різниця між двома прогонами тієї самої моделі — не погіршення (див. noise());
  • перший прогін вердикту не має: він сам стає базою (🔵); коли суддя оцінив замало драфтів, вердикт не визначено (⚠️) —
    краще чесно сказати «не знаємо», ніж видати ✅ чи ❌ за кількома оцінками.

Окремо від вердикту рахуємо спостереження — інші метрики, що просіли (Fact safety, Style, частка драфтів, канали): вони не
міняють вердикт, але показують, де шукати причину, і попереджають про повільний зсув. Чисті функції, без бази й мережі.
"""
from __future__ import annotations

import dataclasses
import math
import os
from dataclasses import dataclass, field
from typing import Any

from .hc_compare import MIN_COVERAGE, SCOPES, SPEC, Comparison, MetricDelta, Row, publishable, summarize
from .hc_fmt import CHANNEL, p_text, pct, short_delta, signed_pp
from .hc_fmt import value as fmt_value

PASSED, FAILED, BASELINE, UNKNOWN = "passed", "failed", "baseline", "unknown"
RANK = {PASSED: 0, BASELINE: 0, UNKNOWN: 1, FAILED: 2}
# (значок, заголовок у звіті, коротко для повідомлення)
LABELS = {PASSED: ("✅", "Модель пройшла", "модель пройшла"), FAILED: ("❌", "Модель не пройшла", "модель не пройшла"),
          BASELINE: ("🔵", "Перший прогін: це база для наступних порівнянь", "перший прогін, це база для порівнянь"),
          UNKNOWN: ("⚠️", "Вердикт не визначено", "вердикт не визначено")}
PRIMARY = "publishable_rate"
DEFAULT_THRESHOLD_PP = 5.0
ERROR_WARN, ERROR_FAIL = 0.10, 0.25  # частка статей, на які модель не відповіла (збій чи таймаут RunPod)
CHANNEL_FACTOR = 1.5  # у одному каналі лише 50 статей — те саме відхилення там шумніше
MIN_PAIRS = 10  # менше статей, що мають оцінку в обох прогонах, — для спостережень зарано
STYLE_AVG_WARN = 0.3  # середній Style (1–5) просів на стільки балів


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def noise_pp() -> float:
    return _f("HEALTHCHECK_NOISE_PP", 0.0)


def threshold_pp() -> float:
    """Поріг падіння «можна публікувати» в п. п.: HEALTHCHECK_THRESHOLD_PP (5), але не менше за шум моделі."""
    return max(_f("HEALTHCHECK_THRESHOLD_PP", DEFAULT_THRESHOLD_PP), noise_pp())


@dataclass
class Verdict:
    status: str  # passed | failed | baseline | unknown
    reason: str  # одне речення: що вийшло і чому такий вердикт
    value: float | None = None  # «можна публікувати» зараз, частка
    base_value: float | None = None  # ... в останньому успішному прогоні
    base_run: str | None = None
    threshold_pp: float = DEFAULT_THRESHOLD_PP  # поріг, який діяв (уже з урахуванням шуму)
    noise_pp: float = 0.0
    problems: list[str] = field(default_factory=list)  # застереження до самого вимірювання (збої моделі тощо)
    observations: list[str] = field(default_factory=list)  # інші метрики, що просіли: не впливають на вердикт

    @property
    def delta_pp(self) -> float | None:
        """Зміна «можна публікувати» в п. п. (від'ємне — падіння); None — порівнювати нема з чим."""
        return None if self.value is None or self.base_value is None else (self.value - self.base_value) * 100

    @property
    def icon(self) -> str:
        return LABELS[self.status][0]

    @property
    def title(self) -> str:
        return LABELS[self.status][1]

    def as_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "delta_pp": self.delta_pp}


# ---------- спостереження: інші метрики ----------

def observation_rules() -> list[tuple[str, float]]:
    """(метрика, наскільки має просісти) в одиницях метрики: частки — у частках, Style — у балах."""
    warn = _f("HEALTHCHECK_WARN_PP", 8) / 100
    return [(PRIMARY, warn), ("fact_ok_rate", warn), ("style_45_rate", warn), ("drafted_rate", warn),
            ("style_avg", STYLE_AVG_WARN)]


def _observation(d: MetricDelta, scope: str, against: str, ref_run: str) -> str:
    where = "" if scope == "all" else f", {CHANNEL[scope]}"
    delta = short_delta(d) + (" п. п." if d.share else "")
    ref = "останнього успішного прогону" if against == "base" else "еталонного прогону"
    return (f"{d.label}{where}: {fmt_value(d.key, d.base)} → {fmt_value(d.key, d.new)} ({delta}, p = {p_text(d)}) "
            f"відносно {ref} {ref_run}")


def observations(c: Comparison, against: str) -> list[str]:
    """Метрики, що просіли помітно (не менше за поріг) і не схоже на випадок (знаковий тест по статтях). Сама «можна
    публікувати» по всьому набору тут не береться: це і є вердикт."""
    alpha = _f("HEALTHCHECK_ALPHA_WARN", 0.2)
    found = []
    for scope in SCOPES:
        factor = 1.0 if scope == "all" else CHANNEL_FACTOR
        for key, warn in observation_rules():
            if key == PRIMARY and scope == "all":
                continue
            d = c.delta(key, scope)
            if d.worsening is None or d.p is None or d.pairs < MIN_PAIRS:
                continue
            if d.worsening >= warn * factor and d.p <= alpha:
                found.append(_observation(d, scope, against, c.base_run))
    return found


# ---------- вердикт ----------

def _problems(new: dict[str, Any], run_id: str | None) -> list[str]:
    """Те, що не залежить від бази, але вимір робить сумнівним: чи відповідала модель і чи дооцінив суддя."""
    out = []
    err = new["error_rate"]
    if err is not None and ERROR_WARN <= err < ERROR_FAIL:
        resume = f' (догенерувати: POST /health-check {{"resume": "{run_id}"}})' if run_id else ""
        out.append(f"Модель не відповіла на {new['errors']} з {new['n']} статей (збій чи таймаут RunPod){resume}")
    if new["judge_errors"]:
        out.append(f"Суддя не зміг оцінити {new['judge_errors']} драфтів (повторити: POST /health-check/runs/<id>/evaluate)")
    return out


def decide(summary: dict[str, dict[str, Any]], base: Comparison | None, baseline: Comparison | None = None, *,
           run_id: str | None = None) -> Verdict:
    """Вердикт прогону. summary — summarize_scopes нового прогону; base — порівняння з останнім УСПІШНИМ прогоном
    (None — його немає); baseline — з еталонним, лише для спостережень."""
    new = summary["all"]
    thr = threshold_pp()
    v = Verdict(UNKNOWN, "", threshold_pp=thr, noise_pp=noise_pp(), problems=_problems(new, run_id))
    v.value = new[PRIMARY]
    for c, against in ((base, "base"), (baseline, "baseline")):
        if c:
            v.observations += observations(c, against)
    n, drafted, cov, err = new["n"], new["drafted"], new["coverage"], new["error_rate"]
    if not n:
        v.reason = "В еталонному наборі немає статей"
    elif not drafted:
        v.status, v.reason = FAILED, f"Модель не дала жодного драфта на {n} статей"
    elif err is not None and err >= ERROR_FAIL:
        v.status = FAILED
        v.reason = f"Модель не відповіла на {new['errors']} з {n} статей (збій чи таймаут RunPod): це не модель, що пише пости"
    elif cov is not None and cov < MIN_COVERAGE:
        v.reason = (f"Суддя оцінив лише {pct(cov)} драфтів ({new['judged']} з {drafted}): за такими даними вердикт "
                    "не ставимо — дооцініть прогін (POST /health-check/runs/<id>/evaluate)")
    elif v.value is None:
        v.reason = "Жодного оціненого драфта: нема за чим ставити вердикт"
    elif base is None:
        v.status = BASELINE
        v.reason = f"Можна публікувати: {pct(v.value)}. Це перший успішний прогін — з ним порівнюватимемо наступні."
    else:
        v.base_run, v.base_value = base.base_run, base.base["all"][PRIMARY]
        base_cov = base.base["all"]["coverage"]
        if v.base_value is None or (base_cov is not None and base_cov < MIN_COVERAGE):
            v.reason = (f"Прогін {base.base_run}, з яким порівнюємо, оцінено лише на {pct(base_cov)}: вердикт не ставимо "
                        "— дооцініть його (POST /health-check/runs/<id>/evaluate)")
        else:
            drop_pp = (v.base_value - v.value) * 100
            v.status = FAILED if round(drop_pp, 6) > round(thr, 6) else PASSED
            change = f"{signed_pp(v.value - v.base_value)} п. п." if round(drop_pp, 6) != 0 else "без змін"
            tail = (f"впало більше за поріг {thr:g} п. п." if v.status == FAILED
                    else f"падіння в межах порога {thr:g} п. п." if drop_pp > 0 else "не впало")
            v.reason = (f"Можна публікувати: {pct(v.value)} проти {pct(v.base_value)} в останньому успішному прогоні "
                        f"{base.base_run} ({change}) — {tail}")
    return v


# ---------- шум моделі ----------

def noise(a: list[Row], b: list[Row]) -> dict[str, Any]:
    """Наскільки результат «скаче» сам: два прогони тієї самої моделі на тих самих статтях. Поріг вердикту не має бути
    меншим за різницю «можна публікувати» між ними. Два прогони — це лише оцінка; чим більше пар, тим точніше."""
    sa, sb = summarize(a), summarize(b)
    b_by = {r.article_id: r for r in b}
    flips = pairs = 0
    for r in a:
        other = b_by.get(r.article_id)
        pa, pb = publishable(r), publishable(other) if other else None
        if pa is not None and pb is not None:
            pairs += 1
            flips += pa != pb
    metrics = {}
    for key in (PRIMARY, "fact_ok_rate", "style_45_rate", "style_avg"):
        x, y = sa[key], sb[key]
        diff = None if x is None or y is None else abs(x - y) * (100 if SPEC[key].share else 1)
        metrics[key] = {"a": x, "b": y, "diff": diff}  # diff: п. п. для часток, бали для середніх
    primary = metrics[PRIMARY]["diff"]
    return {"metrics": metrics, "pairs": pairs, "flips": flips,
            "recommended_pp": None if primary is None else math.ceil(round(primary, 6))}
