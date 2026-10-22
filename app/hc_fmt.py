"""Health Check: як показуємо числа в звіті, вердикті й повідомленні (відсотки, зміни в п. п., p-значення). Без бази й мережі."""
from __future__ import annotations

from typing import Any

from .hc_compare import SPEC, MetricDelta

CHANNEL = {"fb": "FB", "x": "X"}


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def num(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}".replace(".", ",")


def minus(text: str) -> str:
    return text.replace("-", "−")


def trend(d: MetricDelta) -> str:
    """▲ стало краще, ▼ гірше (з огляду на те, чого метриці «хочеться»), ≈ без змін."""
    w = d.worsening
    if w is None:
        return ""
    return "≈" if abs(w) < 0.005 else "▼" if w > 0 else "▲"


def signed_pp(delta: float | None) -> str:
    """Зміна частки в п. п. зі знаком: +1, −5, 0 (дані в частках: −0,05 → −5)."""
    if delta is None:
        return "—"
    text = f"{delta * 100:+.0f}"
    return "0" if text.strip("+-0") == "" else minus(text)


def short_delta(d: MetricDelta) -> str:
    """−6 (частки — у п. п.) або +0,10 (середні)."""
    if d.delta is None:
        return "—"
    if d.share:
        return signed_pp(d.delta)
    text = f"{d.delta:+.2f}".replace(".", ",")
    return "0" if text.strip("+-0,") == "" else minus(text)  # +0,00 / −0,00 — це просто нуль


def delta_text(d: MetricDelta) -> str:
    if d.delta is None:
        return "—"
    return f"{short_delta(d)}{' п. п.' if d.share else ''} {trend(d)}".strip()


def p_text(d: MetricDelta) -> str:
    return "—" if d.p is None else "<0,01" if d.p < 0.01 else num(d.p)


def value(key: str, v: float | None) -> str:
    return pct(v) if SPEC[key].share else num(v)


def run_reason(run: dict[str, Any]) -> str:
    """Чому запущено прогін: примітка того, хто запускав вручну («змінили промпт»), інакше — за розкладом чи вручну."""
    if run.get("note"):
        return str(run["note"])
    return {"schedule": "за розкладом", "retry": "повтор за розкладом: попередній прогін упав"}.get(run.get("trigger") or "",
                                                                                                  "вручну")
