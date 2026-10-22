"""Health Check, пункт 6: сповіщення про результат прогону. Рішення «чи пройшла модель» ухвалює hc_verdict; тут — що і коли
надсилати.

Ритм: повідомлення приходить після КОЖНОГО завершеного прогону — раз на два тижні за розкладом (HEALTHCHECK_EVERY_DAYS=14) і
після ручного запуску, коли команда змінила щось у тому, як пишуться пости, і хоче перевірити якість (POST /health-check з
полем note — воно потрапляє в повідомлення як причина запуску). У повідомленні: вердикт, «можна публікувати» проти останнього
успішного прогону, версія моделі, причина запуску, посилання на звіт. HEALTHCHECK_ALERT_MODE=failed — писати лише коли модель
не пройшла чи вердикт не визначено. Одне повідомлення на кожен вердикт прогону: перебудова звіту нових не спричиняє, а коли
вердикт змінився (не визначено → пройшла після дооцінки) — надсилається ще раз.

Куди: журнал (завжди), Telegram і вебхук — див. notify.py.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

from . import hc_verdict, notify, pg
from .hc_compare import Comparison
from .hc_fmt import num, pct, run_reason

log = logging.getLogger("hc_alert")

PROBLEM_REPEAT_S = 6 * 3600  # те саме повідомлення про збій не частіше, ніж раз на 6 годин
SHOWN_PROBLEMS, SHOWN_OBSERVATIONS = 2, 3
_recent: dict[str, float] = {}


def mode() -> str:
    """every — після кожного прогону (за замовчуванням); failed — лише коли модель не пройшла чи вердикт не визначено."""
    return "failed" if os.environ.get("HEALTHCHECK_ALERT_MODE", "every").lower() == "failed" else "every"


def message(v: hc_verdict.Verdict, run: dict[str, Any], report_url: str | None, *,
            summary: dict[str, dict[str, Any]] | None = None, base: Comparison | None = None) -> str:
    """Коротко, простим текстом (без розмітки: Telegram і вебхуки по-різному її екранують)."""
    icon, _, short = hc_verdict.LABELS[v.status]
    lines = [f"{icon} Health Check {run['run_id']}: {short}", v.reason]
    if summary:
        new = summary["all"]
        lines.append(f"Fact safety ok {pct(new['fact_ok_rate'])} · Style 4–5 {pct(new['style_45_rate'])} · "
                     f"Style (сер.) {num(new['style_avg'], 1)} · драфтів {new['drafted']} з {new['n']}")
    if base and (base.lost or base.gained):
        lines.append(f"Статті, що змінили «можна публікувати»: −{len(base.lost)} / +{len(base.gained)} (список у звіті)")
    lines += [f"⚠️ {p}" for p in v.problems[:SHOWN_PROBLEMS]]
    lines += [f"• {o}" for o in v.observations[:SHOWN_OBSERVATIONS]]
    if len(v.observations) > SHOWN_OBSERVATIONS:
        lines.append(f"• … і ще {len(v.observations) - SHOWN_OBSERVATIONS}")
    lines += [f"Модель: {run['model_version']}", f"Запуск: {run_reason(run)}",
              f"Звіт: {report_url}" if report_url else f"Звіт: GET /health-check/runs/{run['run_id']}/report (з сервера)"]
    return "\n".join(lines)


def sent_verdict(run_id: str, judge_key: str) -> str | None:
    """Вердикт, про який уже сповіщено (None — ще ні)."""
    with pg._connect() as c:
        r = c.execute("SELECT alert_verdict FROM hc_reports WHERE run_id = %s AND judge_key = %s", (run_id, judge_key)).fetchone()
    return r[0] if r else None


async def deliver(run_id: str, judge_key: str, v: hc_verdict.Verdict, run: dict[str, Any], report_url: str | None, *,
                  summary: dict[str, dict[str, Any]] | None = None, base: Comparison | None = None,
                  force: bool = False) -> dict[str, bool] | None:
    """Сповістити про результат прогону. None — сповіщати не треба (режим «лише збої» або про цей вердикт уже сповіщено).
    Стан (alert_verdict, alert_at) пишемо в hc_reports лише коли хоч один канал справді отримав повідомлення, інакше
    наступна спроба (перебудова звіту) відправить ще раз."""
    if mode() == "failed" and hc_verdict.RANK[v.status] == 0 and not force:
        return None
    if not force and sent_verdict(run_id, judge_key) == v.status:
        return None
    text = message(v, run, report_url, summary=summary, base=base)
    level = {hc_verdict.FAILED: log.error, hc_verdict.UNKNOWN: log.warning}.get(v.status, log.info)
    level("Health Check %s: %s\n%s", run_id, v.status, text)
    sent = await notify.send(text, verdict=v.status, run_id=run_id, report_url=report_url)
    if any(sent.values()):
        with pg._connect() as c:
            c.execute("UPDATE hc_reports SET alert_verdict = %s, alert_at = now() WHERE run_id = %s AND judge_key = %s",
                      (v.status, run_id, judge_key))
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
    return await notify.send(text, verdict="problem")
