"""Health Check, пункт 7: звіт по прогону. Один markdown, який однаково читається в терміналі, в GET-ендпоінті й у Notion:
вердикт «пройшла / не пройшла», метрики з порівнянням з останнім успішним прогоном і з еталоном, канали, а якщо модель не
пройшла — що саме погіршилось і на яких статтях (з посиланнями), типи помилок, збої й відмови моделі, час генерації, версія.

render — чиста функція, без бази й мережі. Звіт зберігається в hc_reports (там же стан сповіщення) і, якщо задано
NOTION_HEALTHCHECK_PAGE_ID, потрапляє в Notion НОВИМ РЯДКОМ таблиці «Health Check — прогони» (дата, версія моделі, %,
різниця, вердикт; тіло рядка — цей звіт). Таблицю бекенд створює сам під цією сторінкою один раз і запам'ятовує її id
(sync_state); можна вказати свою готову в NOTION_HEALTHCHECK_DATABASE_ID. У Notion бекенд лише створює: існуючого не
змінює і не видаляє.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from . import hc_verdict, notion, pg
from .hc_compare import SPEC, STYLE_TARGET, Comparison
from .hc_fmt import CHANNEL, delta_text, p_text, pct, run_reason, short_delta, trend, value

log = logging.getLogger("hc_report")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hc_reports (
    run_id        TEXT NOT NULL REFERENCES hc_runs(run_id),
    judge_key     TEXT NOT NULL,
    base_run      TEXT,                   -- останній успішний прогін, з яким порівнювали вердикт
    baseline_run  TEXT,                   -- еталонний прогін, якщо він інший
    verdict       TEXT,                   -- passed | failed | baseline | unknown
    verdict_info  JSONB,                  -- Verdict: числа, поріг, причина, застереження, спостереження
    markdown      TEXT NOT NULL,
    metrics       JSONB,                  -- summarize_scopes нового прогону
    notion_url    TEXT,
    alert_verdict TEXT,                   -- вердикт, про який уже сповіщено (щоб не дублювати)
    alert_at      TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, judge_key)
);
"""

TARGETS = {"style_45_rate": f"≥ {STYLE_TARGET:.0%}"}
SHOWN_LOST, SHOWN_GAINED, SHOWN_REGRESSIONS, SHOWN_IMPROVEMENTS = 15, 8, 10, 8
METRICS = (hc_verdict.PRIMARY, "fact_ok_rate", "style_45_rate", "style_avg", "tier_a_avg", "tier_b_avg", "drafted_rate")


# ---------- форматування ----------

def _clean(text: str) -> str:
    """Назва статті в таблиці чи посиланні: без символів, що ламають markdown."""
    return re.sub(r"\s+", " ", text).replace("[", "(").replace("]", ")").replace("|", "/").strip()


def _when(dt: datetime | None) -> str:
    return "—" if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _seconds(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f} с"


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    line = lambda cells: "| " + " | ".join(cells) + " |"  # noqa: E731
    return [line(header), line(["---"] * len(header)), *(line(r) for r in rows), ""]


# ---------- звіт ----------

def _metrics_table(new: dict[str, Any], base: Comparison | None, baseline: Comparison | None) -> list[str]:
    header = ["Метрика", "Зараз"]
    if base:
        header += [f"Останній успішний ({base.base_run})", "Зміна", "p"]
    if baseline:
        header += [f"Еталон ({baseline.base_run})", "Зміна"]
    header.append("Ціль")
    rows = []
    for key in METRICS:
        label = f"**{SPEC[key].label}**" if key == hc_verdict.PRIMARY else SPEC[key].label
        row = [label, value(key, new[key])]
        if base:
            d = base.delta(key)
            row += [value(key, d.base), delta_text(d), p_text(d)]
        if baseline:
            d = baseline.delta(key)
            row += [value(key, d.base), delta_text(d)]
        row.append(TARGETS.get(key, ""))
        rows.append(row)
    return table(header, rows)


def _channels_table(summary: dict[str, dict[str, Any]], base: Comparison | None) -> list[str]:
    def cell(scope: str, key: str) -> str:
        text = value(key, summary[scope][key])
        if base and (d := base.delta(key, scope)).delta is not None:
            text += f" ({short_delta(d)} {trend(d)})"
        return text

    rows = []
    for scope, name in (("fb", "FB"), ("x", "X")):
        s = summary[scope]
        rows.append([name, str(s["n"]), str(s["drafted"]), str(s["refused"]), str(s["errors"]),
                     cell(scope, hc_verdict.PRIMARY), cell(scope, "fact_ok_rate"), cell(scope, "style_45_rate"),
                     cell(scope, "style_avg")])
    return table(["Канал", "Статей", "Драфтів", "Відмов", "Збоїв", "Можна публікувати", "Fact ok", "Style 4–5",
                  "Style (сер.)"], rows)


def _articles(title: str, items: list[dict[str, Any]], limit: int) -> list[str]:
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


def _rising_failures(new: dict[str, Any], base: Comparison | None) -> list[str]:
    """Типи помилок, які суддя знаходить частіше, ніж в останньому успішному прогоні."""
    if not base:
        return []
    before, now = base.base["all"]["failure_types"], new["failure_types"]
    rising = sorted(((t, before.get(t, 0), n) for t, n in now.items() if n > before.get(t, 0)),
                    key=lambda r: r[1] - r[2])[:8]
    if not rising:
        return ["### Які помилки стали частішими", "", "Жоден тип помилок не став частішим.", ""]
    return ["### Які помилки стали частішими", "",
            *table(["Тип помилки", "Було", "Стало", "Зміна"], [[t, str(b), str(n), f"+{n - b}"] for t, b, n in rising])]


def _what_got_worse(v: hc_verdict.Verdict, new: dict[str, Any], base: Comparison | None) -> list[str]:
    """Розбір для команди: які статті втратили «можна публікувати», які помилки почастішали, потім ширший список змін."""
    if not base:
        return []
    failed = v.status == hc_verdict.FAILED
    out = ["## Що змінилось по статтях", ""]
    out += _articles("Були «можна публікувати», стали «ні»", base.lost, SHOWN_LOST if failed else SHOWN_GAINED)
    out += _articles("Були «ні», стали «можна публікувати»", base.gained, SHOWN_GAINED)
    out += _rising_failures(new, base) if failed else []
    shown = {it["article_id"] for it in base.lost + base.gained}  # ті самі статті двічі не показуємо
    out += _articles("Інші погіршення", [r for r in base.regressions if r["article_id"] not in shown], SHOWN_REGRESSIONS)
    out += _articles("Інші покращення", [r for r in base.improvements if r["article_id"] not in shown], SHOWN_IMPROVEMENTS)
    return out if len(out) > 2 else []


def _failure_types(new: dict[str, Any], base: Comparison | None) -> list[str]:
    now = new["failure_types"]
    if not now:
        return []
    before = base.base["all"]["failure_types"] if base else {}
    header = ["Тип помилки", "Драфтів"] + (["Було"] if base else [])
    rows = [[t, str(n)] + ([str(before.get(t, 0))] if base else []) for t, n in list(now.items())[:10]]
    return ["## Типи помилок", "", *table(header, rows)]


def _self_check(new: dict[str, Any]) -> list[str]:
    if not new["drafted"]:
        return []
    return ["## Самоперевірка моделі", "",
            f"Модель підняла прапорець «не пройшов перевірку» на {new['flagged']} з {new['drafted']} драфтів. "
            f"Коли прапорець був, суддя знайшов Tier A у {pct(new['flag_precision'])} випадків (точність); "
            f"з усіх драфтів, де суддя знайшов Tier A, прапорець мав {pct(new['flag_recall'])} (повнота).", ""]


def _calibration_line(calibration: dict[str, Any] | None) -> str:
    if not calibration:
        return ("- ⚠️ Суддю ще не звірено з людськими оцінками (`python -m app.hc_calibrate`): вердикт міряє те, що бачить "
                "суддя, а не обов'язково те, що побачили б рецензенти.")
    done = calibration["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%d") if calibration.get("created_at") else "—"
    ok = calibration["agreement"] >= calibration["target"]
    return (f"- Звірка судді з людськими оцінками ({done}): збіг по fact safety {pct(calibration['agreement'])} на "
            f"{calibration['decided']} драфтах, ціль ≥ {pct(calibration['target'])} — "
            + ("досягнуто." if ok else "**не досягнуто**: частина «погіршень» може бути помилками судді."))


def _method(v: hc_verdict.Verdict, new: dict[str, Any], judge_key: str, calibration: dict[str, Any] | None) -> list[str]:
    noise = (f"поріг піднято до {v.threshold_pp:g} п. п. через шум моделі" if v.noise_pp > hc_verdict.DEFAULT_THRESHOLD_PP
             else f"шум моделі {v.noise_pp:g} п. п. не більший за поріг" if v.noise_pp
             else "шум моделі ще не виміряно: зробіть два прогони тієї самої моделі й `python -m app.hc_flow noise <run_a> "
                  "<run_b>` — поріг не може бути меншим за цей шум")
    return ["## Як вимірювали", "",
            "- Вердикт: «можна публікувати» (Fact safety ok і Style 4–5) від усіх статей набору — стаття без драфта рахується як "
            f"«ні» — проти останнього успішного прогону. Впало більше за поріг ({v.threshold_pp:g} п. п.) — модель не пройшла. "
            "Інші метрики вердикт не змінюють, вони допомагають знайти причину.",
            f"- Поріг: {noise}.",
            f"- Суддя: `{judge_key}`; оцінено {new['judged']} з {new['drafted']} драфтів ({pct(new['coverage'])}), другу думку "
            f"призначив {new['second_opinion']}, не вдалося оцінити {new['judge_errors']}, готових оцінок узято з попередніх "
            f"прогонів {new['reused']}; вартість оцінки ${new['cost_usd']:.2f}.",
            "- Суддя — дешева модель за інструкцією Антона (`draft-eval`): її оцінки шумніші за людські. Тому порівнюємо "
            "прогони однаковим суддею й одними й тими самими статтями.",
            _calibration_line(calibration),
            "- «Зміна» — різниця часток чи середніх; p — точний знаковий тест по статтях, де результат змінився: що менше, то "
            "менша ймовірність, що це випадок. ▲ краще, ▼ гірше, ≈ без змін.", ""]


def render(run: dict[str, Any], summary: dict[str, dict[str, Any]], judge_key: str, *, verdict: hc_verdict.Verdict,
           base: Comparison | None = None, baseline: Comparison | None = None,
           calibration: dict[str, Any] | None = None) -> str:
    """Звіт по прогону. base — порівняння з останнім УСПІШНИМ прогоном (на ньому стоїть вердикт), baseline — з еталонним
    (якщо він інший), calibration — остання звірка судді з людьми (hc_calibrate.latest)."""
    new = summary["all"]
    out = [f"# Health Check · {run['run_id']} · {_when(run.get('started_at'))[:10]}", "",
           f"> {verdict.icon} **{verdict.title}**", "", f"- {verdict.reason}"]
    out += [f"- ⚠️ {p}" for p in verdict.problems] + [f"- {o}" for o in verdict.observations] + [""]
    ref = base or baseline
    model_base = ((ref.meta.get("base") if ref else None) or {}).get("model_version")
    model_line = f"- Модель: `{run['model_version']}`"
    if model_base:
        model_line += (" — та сама, що в базі" if model_base == run["model_version"]
                       else f" (у базі було `{model_base}`: модель змінилась)")
    out += ["## Прогін", "", model_line, f"- Причина запуску: {run_reason(run)}",
            f"- Запуск: {run['trigger']} · {_when(run.get('started_at'))} → {_when(run.get('finished_at'))} · "
            f"статус {run['status']}",
            f"- Статей у наборі: {new['n']} (FB {summary['fb']['n']}, X {summary['x']['n']}); драфт отримали {new['drafted']}, "
            f"відмов моделі {new['refused']}, збоїв {new['errors']}",
            f"- Час генерації одного драфта: медіана {_seconds(new['gen_median_s'])}, 90% — до {_seconds(new['gen_p90_s'])}, "
            f"найдовший {_seconds(new['gen_max_s'])}", ""]
    warnings = [w for c in (base, baseline) if c for w in c.warnings]
    if warnings:
        out += ["## Застереження", "", *[f"- {w}" for w in dict.fromkeys(warnings)], ""]
    out += ["## Підсумок", "", *_metrics_table(new, base, baseline), "## За каналами", "", *_channels_table(summary, base)]
    out += _what_got_worse(verdict, new, base) + _failure_types(new, base) + _self_check(new)
    out += _method(verdict, new, judge_key, calibration)
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


# ---------- сховище ----------

def init_db() -> None:
    with pg._connect() as c:
        c.execute(SCHEMA)


def save_report(run_id: str, judge_key: str, *, markdown: str, summary: dict[str, Any], base_run: str | None,
                baseline_run: str | None, verdict: hc_verdict.Verdict, notion_url: str | None = None) -> None:
    """Upsert звіту. Стан сповіщення (alert_verdict, alert_at) не чіпаємо: перебудова звіту не має спричиняти повторне."""
    with pg._connect() as c:
        c.execute(
            """INSERT INTO hc_reports (run_id, judge_key, base_run, baseline_run, verdict, verdict_info, markdown, metrics,
                                       notion_url)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s)
               ON CONFLICT (run_id, judge_key) DO UPDATE SET base_run = excluded.base_run,
                 baseline_run = excluded.baseline_run, verdict = excluded.verdict, verdict_info = excluded.verdict_info,
                 markdown = excluded.markdown, metrics = excluded.metrics, created_at = now(),
                 notion_url = coalesce(excluded.notion_url, hc_reports.notion_url)""",
            (run_id, judge_key, base_run, baseline_run, verdict.status,
             json.dumps(verdict.as_dict(), ensure_ascii=False), markdown,
             json.dumps(summary, ensure_ascii=False, default=str), notion_url))


def get_report(run_id: str, judge_key: str | None = None) -> dict[str, Any] | None:
    """Збережений звіт: цього судді, а без ключа — найновіший по прогону."""
    with pg._connect() as c:
        r = c.execute(
            """SELECT run_id, judge_key, base_run, baseline_run, verdict, verdict_info, markdown, notion_url, alert_verdict,
                      alert_at, created_at FROM hc_reports WHERE run_id = %s AND (%s::text IS NULL OR judge_key = %s)
               ORDER BY created_at DESC LIMIT 1""", (run_id, judge_key, judge_key)).fetchone()
    keys = ("run_id", "judge_key", "base_run", "baseline_run", "verdict", "verdict_info", "markdown", "notion_url",
            "alert_verdict", "alert_at", "created_at")
    return dict(zip(keys, r, strict=True)) if r else None


# ---------- Notion: таблиця прогонів ----------

DB_TITLE = "Health Check — прогони"
DB_STATE_KEY = "hc_notion_database_id"
VERDICT_OPTION = {hc_verdict.PASSED: ("✅ Пройшла", "green"), hc_verdict.FAILED: ("❌ Не пройшла", "red"),
                  hc_verdict.BASELINE: ("🔵 База", "blue"), hc_verdict.UNKNOWN: ("⚠️ Не визначено", "yellow")}
TITLE_PROP = "Прогін"
_db_lock = asyncio.Lock()  # одна таблиця на всіх: два прогони, що одночасно доходять до Notion, не створять по таблиці


def database_schema() -> dict[str, Any]:
    pct_, plain = {"number": {"format": "percent"}}, {"number": {"format": "number"}}
    return {TITLE_PROP: {"title": {}}, "Дата": {"date": {}}, "Версія моделі": {"rich_text": {}},
            "Можна публікувати": pct_, "Різниця, п. п.": plain, "Поріг, п. п.": plain,
            "Вердикт": {"select": {"options": [{"name": n, "color": c} for n, c in VERDICT_OPTION.values()]}},
            "База": {"rich_text": {}}, "Fact safety ok": pct_, "Style 4–5": pct_, "Середній Style": plain,
            "Драфтів": plain, "Відмов і збоїв": plain, "Причина запуску": {"rich_text": {}}}


def row_properties(run: dict[str, Any], summary: dict[str, dict[str, Any]], v: hc_verdict.Verdict) -> dict[str, Any]:
    """Рядок таблиці прогонів: дата, версія моделі, %, різниця, вердикт + кілька цифр, щоб бачити динаміку без відкриття."""
    new = summary["all"]
    started = run.get("started_at")
    rich = lambda text: {"rich_text": notion.rich_text(text)}  # noqa: E731
    props: dict[str, Any] = {
        TITLE_PROP: {"title": notion.rich_text(f"{_when(started)[:10]} · {run['run_id']}")},
        "Версія моделі": rich(run["model_version"]), "Вердикт": {"select": {"name": VERDICT_OPTION[v.status][0]}},
        "Поріг, п. п.": {"number": v.threshold_pp}, "Причина запуску": rich(run_reason(run)),
        "Драфтів": {"number": new["drafted"]}, "Відмов і збоїв": {"number": new["refused"] + new["errors"]}}
    if started:
        props["Дата"] = {"date": {"start": started.astimezone(timezone.utc).isoformat()}}
    numbers = {"Можна публікувати": v.value, "Fact safety ok": new["fact_ok_rate"], "Style 4–5": new["style_45_rate"],
               "Середній Style": new["style_avg"]}
    props.update({name: {"number": round(x, 4)} for name, x in numbers.items() if x is not None})
    if v.delta_pp is not None:
        props["Різниця, п. п."] = {"number": round(v.delta_pp, 1)}
    if v.base_run:
        props["База"] = rich(v.base_run)
    return props


async def ensure_database(parent_page_id: str) -> str:
    """Таблиця прогонів у Notion: з NOTION_HEALTHCHECK_DATABASE_ID, інакше збережена раніше, інакше створюємо під сторінкою
    NOTION_HEALTHCHECK_PAGE_ID й запам'ятовуємо її id."""
    if given := os.environ.get("NOTION_HEALTHCHECK_DATABASE_ID", "").replace("-", ""):
        return given
    async with _db_lock:
        if stored := pg.get_state(DB_STATE_KEY):
            return stored
        db = await notion.create_database(parent_page_id, DB_TITLE, database_schema())
        db_id = str(db["id"]).replace("-", "")
        pg.set_state(DB_STATE_KEY, db_id)
        log.info("Notion: створено таблицю «%s» (%s)", DB_TITLE, db_id)
        return db_id


async def fit_properties(database_id: str, props: dict[str, Any]) -> dict[str, Any]:
    """Для таблиці, яку команда створила сама: лишаємо лише властивості, що в ній є і мають той самий тип; заголовок
    пишемо в її title-колонку, як би вона не називалась."""
    schema = (await notion.get_database(database_id)).get("properties", {})
    title = next((name for name, p in schema.items() if p.get("type") == "title"), TITLE_PROP)
    out = {}
    for name, prop in props.items():
        kind = next(iter(prop))
        target = title if kind == "title" else name
        if target in schema and schema[target].get("type") == kind:
            out[target] = prop
    return out


async def publish(run: dict[str, Any], markdown: str, verdict: hc_verdict.Verdict,
                  summary: dict[str, dict[str, Any]]) -> str | None:
    """Новий рядок у таблиці прогонів, тіло рядка — звіт; повертає URL рядка. Не задано ні сторінки, ні таблиці — None."""
    parent = os.environ.get("NOTION_HEALTHCHECK_PAGE_ID", "").replace("-", "")
    own = os.environ.get("NOTION_HEALTHCHECK_DATABASE_ID", "").replace("-", "")
    if not (parent or own):
        return None
    db_id = await ensure_database(parent)
    props = row_properties(run, summary, verdict)
    if own:
        props = await fit_properties(db_id, props)
    body = markdown.split("\n", 1)[1] if markdown.startswith("# ") else markdown  # заголовок звіту — це назва рядка
    page = await notion.create_row(db_id, props, extra_blocks=to_notion_blocks(body))
    return page.get("url")
