"""Health Check, пункт 6: алерт про погіршення. Рішення «чи стали пости гіршими» ухвалює assess() — чиста функція над
порівняннями з hc_compare; deliver() лише доставляє його.

Щоб не кричати даремно, зміна має одночасно бути великою І статистично відрізнятись від випадку: на 100 статтях різниця
в 5 п. п. — це п'ять драфтів, які могли розійтись інакше й без жодної зміни моделі. Пороги (змінні середовища):
  warning  — гірше на HEALTHCHECK_WARN_PP (8) п. п. і p ≤ HEALTHCHECK_ALPHA_WARN (0,2)
  critical — гірше на HEALTHCHECK_CRIT_PP (15) п. п. і p ≤ HEALTHCHECK_ALPHA_CRIT (0,1)
Для окремого каналу (50 статей) пороги ×1,5. Окремо від порівняння: частка статей, на які модель не відповіла (збій чи
таймаут RunPod), і неповна оцінка судді — звіту тоді не можна вірити.

Доставка: журнал (завжди), HEALTHCHECK_ALERT_WEBHOOK (JSON із text і content — Slack, Mattermost, Discord) і Telegram
(HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN + _CHAT_ID). Один рівень на прогін: повторно — лише коли стало гірше.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import hc_report, pg
from .hc_compare import MIN_COVERAGE, SCOPES, Comparison, MetricDelta

log = logging.getLogger("hc_alert")

RANK = {"info": 0, "ok": 0, "warning": 1, "critical": 2}
CHANNEL_FACTOR = 1.5  # у одному каналі лише 50 статей — те саме відхилення там шумніше
MIN_PAIRS = 10  # менше статей, що мають оцінку в обох прогонах, — порівнювати зарано
STYLE_AVG_WARN, STYLE_AVG_CRIT = 0.3, 0.6  # середній Style (1–5), у балах
PROBLEM_REPEAT_S = 6 * 3600  # те саме повідомлення про збій не частіше, ніж раз на 6 годин
_recent: dict[str, float] = {}


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def rules() -> list[tuple[str, float, float]]:
    """(метрика, поріг warning, поріг critical) в одиницях метрики: частки — у частках, Style — у балах."""
    warn, crit = _f("HEALTHCHECK_WARN_PP", 8) / 100, _f("HEALTHCHECK_CRIT_PP", 15) / 100
    return [("fact_ok_rate", warn, crit), ("style_45_rate", warn, crit), ("drafted_rate", warn, crit),
            ("style_avg", STYLE_AVG_WARN, STYLE_AVG_CRIT)]


@dataclass
class Finding:
    level: str  # warning | critical
    text: str


@dataclass
class Assessment:
    level: str  # info (нема з чим порівнювати) | ok | warning | critical
    findings: list[Finding] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [f.text for f in sorted(self.findings, key=lambda f: -RANK[f.level])]


def _reason(d: MetricDelta, scope: str, against: str, ref_run: str) -> str:
    where = "" if scope == "all" else f", {hc_report.CHANNEL[scope]}"
    delta = hc_report.short_delta(d) + (" п. п." if d.share else "")
    ref = "попереднього прогону" if against == "previous" else "еталонного прогону"
    return (f"{d.label}{where}: {hc_report.value(d.key, d.base)} → {hc_report.value(d.key, d.new)} "
            f"({delta}, p = {hc_report.p_text(d)}) відносно {ref} {ref_run}")


def _compare_findings(c: Comparison, against: str) -> list[Finding]:
    alpha_warn, alpha_crit = _f("HEALTHCHECK_ALPHA_WARN", 0.2), _f("HEALTHCHECK_ALPHA_CRIT", 0.1)
    found = []
    for scope in SCOPES:
        factor = 1.0 if scope == "all" else CHANNEL_FACTOR
        for key, warn, crit in rules():
            d = c.delta(key, scope)
            if d.worsening is None or d.p is None or d.pairs < MIN_PAIRS:
                continue
            if d.worsening >= crit * factor and d.p <= alpha_crit:
                level = "critical"
            elif d.worsening >= warn * factor and d.p <= alpha_warn:
                level = "warning"
            else:
                continue
            found.append(Finding(level, _reason(d, scope, against, c.base_run)))
    return found


def _health_findings(summary: dict[str, Any]) -> list[Finding]:
    """Те, що не залежить від бази: чи відповідала модель і чи дооцінив суддя."""
    new, found = summary["all"], []
    err_warn, err_crit = _f("HEALTHCHECK_ERROR_WARN", 0.10), _f("HEALTHCHECK_ERROR_CRIT", 0.25)
    if new["n"] and not new["drafted"]:
        found.append(Finding("critical", f"Модель не дала жодного драфта на {new['n']} статей"))
    elif new["error_rate"] is not None and new["error_rate"] >= err_warn:
        found.append(Finding("critical" if new["error_rate"] >= err_crit else "warning",
                             f"Модель не відповіла на {new['errors']} з {new['n']} статей (збій чи таймаут RunPod)"))
    if new["drafted"] and new["coverage"] is not None and new["coverage"] < MIN_COVERAGE:
        found.append(Finding("warning", f"Суддя оцінив лише {hc_report.pct(new['coverage'])} драфтів "
                                        f"({new['judged']} з {new['drafted']}): цифрам звіту не варто вірити"))
    return found


def assess(prev: Comparison | None, baseline: Comparison | None, summary: dict[str, Any]) -> Assessment:
    findings = _health_findings(summary)
    for c, against in ((prev, "previous"), (baseline, "baseline")):
        if c:
            findings += _compare_findings(c, against)
    level = max((f.level for f in findings), key=lambda lv: RANK[lv], default="ok" if (prev or baseline) else "info")
    return Assessment(level, findings)


# ---------- доставка ----------

def message(a: Assessment, run: dict[str, Any], report_url: str | None) -> str:
    icon, title = hc_report.LEVELS[a.level]
    lines = [f"{icon} Health Check {run['run_id']}: {title[0].lower()}{title[1:]}", *[f"• {r}" for r in a.reasons[:8]]]
    if len(a.reasons) > 8:
        lines.append(f"• … і ще {len(a.reasons) - 8}")
    lines.append(f"Модель: {run['model_version']}")
    lines.append(f"Звіт: {report_url}" if report_url else f"Звіт: GET /health-check/runs/{run['run_id']}/report (з сервера)")
    return "\n".join(lines)


def channels() -> list[str]:
    """Куди бекенд вміє слати алерти (журнал є завжди)."""
    found = []
    if os.environ.get("HEALTHCHECK_ALERT_WEBHOOK"):
        found.append("webhook")
    if os.environ.get("HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN") and os.environ.get("HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID"):
        found.append("telegram")
    return found


async def send(text: str, **extra: Any) -> dict[str, bool]:
    """Надіслати текст у всі налаштовані канали; повертає {канал: чи дійшло}. Збій одного каналу не заважає іншим."""
    sent: dict[str, bool] = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for name in channels():
            try:
                if name == "webhook":
                    r = await client.post(os.environ["HEALTHCHECK_ALERT_WEBHOOK"], json={"text": text, "content": text, **extra})
                else:
                    token = os.environ["HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN"]
                    r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                        "chat_id": os.environ["HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID"], "text": text,
                        "disable_web_page_preview": True})
                r.raise_for_status()
                sent[name] = True
            except Exception as e:  # у тексті помилки може бути адреса вебхука чи токен — у журнал лише тип
                log.warning("алерт Health Check: канал %s не відповів (%s)", name, type(e).__name__)
                sent[name] = False
    return sent


def _min_level() -> str:
    return "critical" if os.environ.get("HEALTHCHECK_ALERT_MIN_LEVEL", "warning").lower() == "critical" else "warning"


def sent_level(run_id: str, judge_key: str) -> str | None:
    with pg._connect() as c:
        r = c.execute("SELECT alert_level FROM hc_reports WHERE run_id = %s AND judge_key = %s", (run_id, judge_key)).fetchone()
    return r[0] if r else None


async def deliver(run_id: str, judge_key: str, a: Assessment, run: dict[str, Any], report_url: str | None, *,
                  force: bool = False) -> dict[str, bool] | None:
    """Сповістити про погіршення. None — сповіщати не треба (рівень нижче порогу або про цей рівень уже сповіщено).
    Стан (alert_level, alert_at) пишемо в hc_reports лише коли хоч один канал справді отримав повідомлення."""
    if RANK[a.level] < RANK[_min_level()]:
        return None
    if not force and RANK[sent_level(run_id, judge_key) or "info"] >= RANK[a.level]:
        return None
    text = message(a, run, report_url)
    (log.error if a.level == "critical" else log.warning)("Health Check %s: %s\n%s", run_id, a.level, text)
    sent = await send(text, level=a.level, run_id=run_id, report_url=report_url)
    if any(sent.values()):
        with pg._connect() as c:
            c.execute("UPDATE hc_reports SET alert_level = %s, alert_at = now() WHERE run_id = %s AND judge_key = %s",
                      (a.level, run_id, judge_key))
    return sent


async def notify_problem(subject: str, details: str = "") -> dict[str, bool]:
    """Збій самого Health Check (прогін упав, суддя не відповідає: ключ, кошти): без цього тижні минали б мовчки.
    Те саме повідомлення — не частіше, ніж раз на PROBLEM_REPEAT_S."""
    text = f"🔴 {subject}" + (f"\n{details[:500]}" if details else "")
    key = hashlib.sha256(text.encode()).hexdigest()
    now = time.monotonic()
    if now - _recent.get(key, -PROBLEM_REPEAT_S) < PROBLEM_REPEAT_S:
        return {}
    _recent[key] = now
    log.error("Health Check: %s", text)
    return await send(text, level="critical")
