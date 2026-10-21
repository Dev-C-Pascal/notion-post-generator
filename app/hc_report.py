"""Health Check, пункт 5: звіт по прогону. Один markdown, який однаково читається в терміналі, в GET-ендпоінті й у Notion:
вердикт, метрики з порівнянням з попереднім прогоном і з базою, канали, статті, що погіршились чи покращились, типи
помилок, якість самоперевірки моделі і чесні застереження щодо самого вимірювання (суддя — дешева модель, вибірка 100).

render — чиста функція, без бази й мережі. Звіт зберігається в hc_reports (там же стан алерту) і, якщо задано
NOTION_HEALTHCHECK_PAGE_ID, публікується підсторінкою під цією сторінкою. У Notion бекенд лише створює нову сторінку:
існуючого не змінює і не видаляє.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from . import notion, pg
from .hc_compare import SPEC, STYLE_TARGET, Comparison, MetricDelta

log = logging.getLogger("hc_report")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hc_reports (
    run_id       TEXT NOT NULL REFERENCES hc_runs(run_id),
    judge_key    TEXT NOT NULL,
    base_run     TEXT,                    -- з чим порівнювали: попередній завершений прогін
    baseline_run TEXT,                    -- еталонний прогін, якщо він інший, ніж попередній
    level        TEXT,                    -- info (перший прогін) | ok | warning | critical
    reasons      JSONB,                   -- чому такий рівень
    markdown     TEXT NOT NULL,
    metrics      JSONB,                   -- summarize_scopes нового прогону
    notion_url   TEXT,
    alert_level  TEXT,                    -- рівень, про який уже сповіщено (щоб не дублювати)
    alert_at     TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, judge_key)
);
"""

LEVELS = {"ok": ("✅", "Погіршень не виявлено"), "warning": ("⚠️", "Є ознаки погіршення"), "critical": ("🔴", "Погіршення"),
          "info": ("ℹ️", "Перший прогін: це база для наступних порівнянь")}
CHANNEL = {"fb": "FB", "x": "X"}
TARGETS = {"style_45_rate": f"≥ {STYLE_TARGET:.0%}"}
SHOWN_REGRESSIONS, SHOWN_IMPROVEMENTS = 15, 8


# ---------- форматування ----------

def pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def num(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}".replace(".", ",")


def _minus(text: str) -> str:
    return text.replace("-", "−")


def trend(d: MetricDelta) -> str:
    """▲ стало краще, ▼ гірше (з огляду на те, чого метриці «хочеться»), ≈ без змін."""
    w = d.worsening
    if w is None:
        return ""
    return "≈" if abs(w) < 0.005 else "▼" if w > 0 else "▲"


def short_delta(d: MetricDelta) -> str:
    """−6 (частки — у п. п.) або +0,10 (середні)."""
    if d.delta is None:
        return "—"
    text = f"{d.delta * 100:+.0f}" if d.share else f"{d.delta:+.2f}".replace(".", ",")
    return "0" if text.strip("+-0,") == "" else _minus(text)  # +0 / −0,00 — це просто нуль


def delta_text(d: MetricDelta) -> str:
    if d.delta is None:
        return "—"
    return f"{short_delta(d)}{' п. п.' if d.share else ''} {trend(d)}".strip()


def p_text(d: MetricDelta) -> str:
    return "—" if d.p is None else "<0,01" if d.p < 0.01 else num(d.p)


def value(key: str, v: float | None) -> str:
    return pct(v) if SPEC[key].share else num(v)


def _clean(text: str) -> str:
    """Назва статті в таблиці чи посиланні: без символів, що ламають markdown."""
    return re.sub(r"\s+", " ", text).replace("[", "(").replace("]", ")").replace("|", "/").strip()


def _when(dt: datetime | None) -> str:
    return "—" if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    line = lambda cells: "| " + " | ".join(cells) + " |"  # noqa: E731
    return [line(header), line(["---"] * len(header)), *(line(r) for r in rows), ""]


# ---------- звіт ----------

METRICS = ("fact_ok_rate", "style_45_rate", "publishable_rate", "style_avg", "tier_a_avg", "tier_b_avg", "drafted_rate")


def _metrics_table(new: dict[str, Any], prev: Comparison | None, base: Comparison | None) -> list[str]:
    header = ["Метрика", "Зараз"]
    if prev:
        header += [f"Попередній ({prev.base_run})", "Зміна", "p"]
    if base:
        header += [f"База ({base.base_run})", "Зміна"]
    header.append("Ціль")
    rows = []
    for key in METRICS:
        row = [SPEC[key].label, value(key, new[key])]
        if prev:
            d = prev.delta(key)
            row += [value(key, d.base), delta_text(d), p_text(d)]
        if base:
            d = base.delta(key)
            row += [value(key, d.base), delta_text(d)]
        row.append(TARGETS.get(key, ""))
        rows.append(row)
    return table(header, rows)


def _channels_table(summary: dict[str, dict[str, Any]], prev: Comparison | None) -> list[str]:
    def cell(scope: str, key: str) -> str:
        text = value(key, summary[scope][key])
        if prev and (d := prev.delta(key, scope)).delta is not None:
            text += f" ({short_delta(d)} {trend(d)})"
        return text

    rows = []
    for scope, name in (("fb", "FB"), ("x", "X")):
        s = summary[scope]
        rows.append([name, str(s["n"]), str(s["drafted"]), str(s["refused"]), str(s["errors"]), cell(scope, "fact_ok_rate"),
                     cell(scope, "style_45_rate"), cell(scope, "style_avg"), cell(scope, "tier_a_avg")])
    return table(["Канал", "Статей", "Драфтів", "Відмов", "Збоїв", "Fact ok", "Style 4–5", "Style (сер.)",
                  "Tier A на драфт"], rows)


def _changes(title: str, items: list[dict[str, Any]], limit: int) -> list[str]:
    if not items:
        return []
    out = [f"### {title} ({len(items)})"]
    for it in items[:limit]:
        title_link = f"[{_clean(it['title']) or it['article_id']}]({it['url']})"
        out.append(f"- **{CHANNEL.get(it['channel'], it['channel'])}** {title_link} — "
                   f"{it['what']} ({it['before']} → {it['after']})")
    if len(items) > limit:
        out.append(f"- … і ще {len(items) - limit}")
    return [*out, ""]


def _failure_types(new: dict[str, Any], prev: Comparison | None) -> list[str]:
    now = new["failure_types"]
    if not now:
        return []
    before = prev.base["all"]["failure_types"] if prev else {}
    header = ["Тип помилки", "Драфтів"] + (["Було"] if prev else [])
    rows = [[t, str(n)] + ([str(before.get(t, 0))] if prev else []) for t, n in list(now.items())[:10]]
    return ["## Типи помилок", "", *table(header, rows)]


def _self_check(new: dict[str, Any]) -> list[str]:
    if not new["drafted"]:
        return []
    out = ["## Самоперевірка моделі", "",
           f"Модель підняла прапорець «не пройшов перевірку» на {new['flagged']} з {new['drafted']} драфтів. "
           f"Коли прапорець був, суддя знайшов Tier A у {pct(new['flag_precision'])} випадків (точність); "
           f"з усіх драфтів, де суддя знайшов Tier A, прапорець мав {pct(new['flag_recall'])} (повнота).", ""]
    return out


def _judge(new: dict[str, Any], judge_key: str) -> list[str]:
    cov = pct(new["coverage"])
    return ["## Як вимірювали", "",
            f"- Суддя: `{judge_key}`; оцінено {new['judged']} з {new['drafted']} драфтів ({cov}), другу думку призначив "
            f"{new['second_opinion']}, не вдалося оцінити {new['judge_errors']}, готових оцінок узято з попередніх прогонів "
            f"{new['reused']}; вартість оцінки ${new['cost_usd']:.2f}.",
            "- Суддя — дешева модель за інструкцією Антона (`draft-eval`): її оцінки шумніші за людські. Тому порівнюємо "
            "прогони однаковим суддею й одними й тими самими 100 статтями.",
            "- «Зміна» — різниця часток чи середніх; p — точний знаковий тест по статтях, де результат змінився: "
            "що менше, то менша ймовірність, що це випадок. ▲ краще, ▼ гірше, ≈ без змін.",
            "- Різниця в кілька п. п. на 100 статтях — у межах шуму; дивіться на p і на список статей вище.", ""]


def render(run: dict[str, Any], summary: dict[str, dict[str, Any]], judge_key: str, *, prev: Comparison | None = None,
           baseline: Comparison | None = None, level: str | None = None, reasons: list[str] | None = None) -> str:
    """Звіт по прогону. prev — порівняння з попереднім завершеним прогоном, baseline — з еталонним (якщо він інший)."""
    new = summary["all"]
    out = [f"# Health Check · {run['run_id']} · {_when(run.get('started_at'))[:10]}", ""]
    if level in LEVELS:
        icon, text = LEVELS[level]
        out += [f"> {icon} **{text}**", ""]
        out += [f"- {r}" for r in reasons or []] + ([""] if reasons else [])
    out += ["## Прогін", ""]
    ref = prev or baseline
    model_base = ((ref.meta.get("base") if ref else None) or {}).get("model_version")
    model_line = f"- Модель: `{run['model_version']}`"
    if model_base:
        model_line += (" — та сама, що в базі" if model_base == run["model_version"]
                       else f" (у базі було `{model_base}`: модель змінилась)")
    out += [model_line,
            f"- Запуск: {run['trigger']} · {_when(run.get('started_at'))} → {_when(run.get('finished_at'))} · "
            f"статус {run['status']}",
            f"- Статей у наборі: {new['n']} (FB {summary['fb']['n']}, X {summary['x']['n']}); драфт отримали {new['drafted']}, "
            f"відмов моделі {new['refused']}, збоїв {new['errors']}", ""]
    warnings = [w for c in (prev, baseline) if c for w in c.warnings]
    if warnings:
        out += ["## Застереження", "", *[f"- {w}" for w in dict.fromkeys(warnings)], ""]
    out += ["## Підсумок", "", *_metrics_table(new, prev, baseline), "## За каналами", "", *_channels_table(summary, prev)]
    if prev:
        out += _changes("Погіршилось", prev.regressions, SHOWN_REGRESSIONS) + _changes("Покращилось", prev.improvements,
                                                                                         SHOWN_IMPROVEMENTS)
    out += _failure_types(new, prev) + _self_check(new) + _judge(new, judge_key)
    return "\n".join(out).rstrip() + "\n"


# ---------- Notion: markdown → блоки ----------

_INLINE = re.compile(r"\*\*(.+?)\*\*|\[([^\]]+)\]\((https?://[^)\s]+)\)|`([^`]+)`")


def _text(content: str, *, bold: bool = False, code: bool = False, link: str | None = None) -> list[dict]:
    out = []
    for i in range(0, max(len(content), 1), notion.TEXT_LIMIT):  # ліміт Notion: 2000 символів на фрагмент
        item: dict[str, Any] = {"type": "text", "text": {"content": content[i:i + notion.TEXT_LIMIT]}}
        if link:
            item["text"]["link"] = {"url": link}
        if bold or code:
            item["annotations"] = {"bold": bold, "code": code}
        out.append(item)
    return out


def inline(text: str) -> list[dict]:
    """**жирний**, [текст](https://…) і `код` → rich_text Notion; решта — звичайний текст."""
    parts: list[dict] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            parts += _text(text[pos:m.start()])
        if m.group(1) is not None:
            parts += _text(m.group(1), bold=True)
        elif m.group(2) is not None:
            parts += _text(m.group(2), link=m.group(3))
        else:
            parts += _text(m.group(4), code=True)
        pos = m.end()
    return parts + (_text(text[pos:]) if pos < len(text) else [])


def _block(kind: str, text: str, **extra: Any) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": inline(text), **extra}}


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def to_notion_blocks(markdown: str) -> list[dict]:
    """Підмножина markdown, яку пише render: заголовки #–###, «- » списки, «> » виноска, таблиці, абзаци."""
    blocks: list[dict] = []
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = _cells(lines[i])
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):  # рядок-розділювач таблиці пропускаємо
                    rows.append(cells)
                i += 1
            width = len(rows[0])
            blocks.append({"object": "block", "type": "table", "table": {
                "table_width": width, "has_column_header": True, "has_row_header": False,
                "children": [{"object": "block", "type": "table_row",
                              "table_row": {"cells": [inline(c) for c in (r + [""] * width)[:width]]}} for r in rows]}})
            continue
        i += 1
        if not line.strip():
            continue
        if m := re.match(r"(#{1,3}) (.+)", line):
            blocks.append(_block(f"heading_{len(m.group(1))}", m.group(2)))
        elif line.startswith("- "):
            blocks.append(_block("bulleted_list_item", line[2:]))
        elif line.startswith("> "):
            icon, _, rest = line[2:].partition(" ")
            blocks.append(_block("callout", rest, icon={"type": "emoji", "emoji": icon}))
        else:
            blocks.append(_block("paragraph", line))
    return blocks


# ---------- сховище й публікація ----------

def init_db() -> None:
    with pg._connect() as c:
        c.execute(SCHEMA)


def save_report(run_id: str, judge_key: str, *, markdown: str, summary: dict[str, Any], base_run: str | None,
                baseline_run: str | None, level: str | None, reasons: list[str], notion_url: str | None = None) -> None:
    """Upsert звіту. Стан алерту (alert_level, alert_at) не чіпаємо: перебудова звіту не має спричиняти повторний алерт."""
    with pg._connect() as c:
        c.execute(
            """INSERT INTO hc_reports (run_id, judge_key, base_run, baseline_run, level, reasons, markdown, metrics, notion_url)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s)
               ON CONFLICT (run_id, judge_key) DO UPDATE SET base_run = excluded.base_run,
                 baseline_run = excluded.baseline_run, level = excluded.level, reasons = excluded.reasons,
                 markdown = excluded.markdown, metrics = excluded.metrics, created_at = now(),
                 notion_url = coalesce(excluded.notion_url, hc_reports.notion_url)""",
            (run_id, judge_key, base_run, baseline_run, level, json.dumps(reasons, ensure_ascii=False), markdown,
             json.dumps(summary, ensure_ascii=False, default=str), notion_url))


def get_report(run_id: str, judge_key: str | None = None) -> dict[str, Any] | None:
    """Збережений звіт: цього судді, а без ключа — найновіший по прогону."""
    with pg._connect() as c:
        r = c.execute(
            """SELECT run_id, judge_key, base_run, baseline_run, level, reasons, markdown, notion_url, alert_level, alert_at,
                      created_at FROM hc_reports WHERE run_id = %s AND (%s::text IS NULL OR judge_key = %s)
               ORDER BY created_at DESC LIMIT 1""", (run_id, judge_key, judge_key)).fetchone()
    keys = ("run_id", "judge_key", "base_run", "baseline_run", "level", "reasons", "markdown", "notion_url", "alert_level",
            "alert_at", "created_at")
    return dict(zip(keys, r, strict=True)) if r else None


async def publish(run_id: str, markdown: str, level: str | None) -> str | None:
    """Нова підсторінка зі звітом під NOTION_HEALTHCHECK_PAGE_ID; повертає її URL. Не задано — None. Лише створення."""
    parent = os.environ.get("NOTION_HEALTHCHECK_PAGE_ID", "").replace("-", "")
    if not parent:
        return None
    icon = LEVELS.get(level or "", ("",))[0]
    title = f"{icon} {markdown.split(chr(10), 1)[0].removeprefix('# ')}".strip()
    page = await notion.create_page(parent, title, to_notion_blocks(markdown))
    return page.get("url")
