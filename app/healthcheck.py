"""Health Check: чи не стали пости гіршими після зміни моделі. Тут — еталонний набір і окремий режим прогону.

Еталонний набір (hc_articles): 100 статей з історії, FB і X навпіл. Лише ті, на драфт яких рецензент у таблиці MVP
поставив «Article relevance = yes». Тексти скопійовано з бази Андрія в postgen: між прогонами вони не змінюються,
тож різниця в результатах — це різниця моделі, а не новин. Набір збирається один раз і не перезаписується:
    docker compose exec -T app python -m app.healthcheck build-set

Прогін (hc_runs, hc_drafts): модель пише драфт для кожної еталонної статті під її канал (fb → uk, x → en).
Окремо від кнопок: статті не бронює (used_articles), у Notion не пише, рядок статусу не чіпає. Генерує у власному
пулі на HEALTHCHECK_PARALLEL потоків і бере місце лише тоді, коли кнопкам лишається вільне в їхньому пулі.
Запуск — вручну (POST /health-check з сервера, можна вказати endpoint_id і mode моделі) або за розкладом
(HEALTHCHECK_EVERY_DAYS, напр. 7; не задано — розкладу немає). Прогін, обірваний деплоєм, стає interrupted;
{"resume": run_id} догенеровує лише те, чого бракує (і статті зі збоєм).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from . import comms, hc_eval, hc_report, notion, pg, pipeline
from .llm import GroundingRefused, ModelClient, RunPodModelClient
from .models import Article, Draft

log = logging.getLogger("healthcheck")

PER_CHANNEL = 50  # 100 статей, FB і X навпіл
CHANNELS = ("fb", "x")
RELEVANCE_PROP = "Article relevance"
PARALLEL = int(os.environ.get("HEALTHCHECK_PARALLEL", "2"))
EVERY_DAYS = float(os.environ.get("HEALTHCHECK_EVERY_DAYS") or 0)  # 0 — без розкладу, лише вручну
WAIT_S = 5.0  # як часто перевіряти, чи звільнилось місце від кнопок
SCHEDULE_CHECK_S = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS hc_articles (
    article_id     TEXT PRIMARY KEY,
    channel        TEXT NOT NULL,            -- fb | x: під який канал писати драфт
    url            TEXT NOT NULL,
    title          TEXT NOT NULL,
    text           TEXT NOT NULL,            -- копія з comms.core.article на момент збору набору
    source         TEXT NOT NULL,
    published_at   TEXT NOT NULL,
    notion_page_id TEXT,                     -- рядок MVP, де рецензент поставив Article relevance = yes
    added_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hc_runs (
    run_id        TEXT PRIMARY KEY,
    trigger       TEXT NOT NULL,             -- manual | schedule
    model_version TEXT NOT NULL,             -- runpod-<endpoint>:<mode>
    status        TEXT NOT NULL,             -- running | done | interrupted | failed
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS hc_drafts (
    run_id      TEXT NOT NULL REFERENCES hc_runs(run_id),
    article_id  TEXT NOT NULL REFERENCES hc_articles(article_id),
    outcome     TEXT NOT NULL,               -- ok | needs_manual_review | refused | error
    lang        TEXT,
    text        TEXT,                        -- пост; для needs_manual_review — draft_for_review (НЕ перевірено)
    extraction  JSONB,                       -- facts_used, facts_rejected, verification… (як drafts.extraction)
    error       TEXT,
    duration_ms INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, article_id)
);
"""

_POOL = ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="healthcheck")
_active = 0  # скільки генерацій Health Check іде зараз
_current: str | None = None  # прогін, що йде зараз: одночасно лише один
_tasks: set[asyncio.Task] = set()  # посилання на фонові прогони, щоб їх не зібрав GC


@dataclass
class RefArticle:
    article: Article
    channel: str


class AlreadyRunning(RuntimeError):
    pass


# ---------- еталонний набір ----------

def _norm(page_id: str | None) -> str:
    return (page_id or "").replace("-", "")


def _order(article_id: str) -> str:
    """Стабільний «випадковий» порядок: той самий набір при кожному збиранні, без перекосу в бік старих чи нових."""
    return hashlib.sha256(article_id.encode()).hexdigest()


def pick_reference(candidates: list[tuple[str, str, str]], relevant_pages: set[str], texts: dict[str, Article],
                   per_channel: int = PER_CHANNEL) -> list[tuple[str, str, str]]:
    """candidates — (article_id, channel, notion_page_id) з історії. Беремо лише релевантні за рецензентом і з
    повним текстом, по per_channel на канал. Замало — помилка: неповний набір дав би інші відсотки."""
    chosen: list[tuple[str, str, str]] = []
    for ch in CHANNELS:
        ok = sorted((c for c in candidates if c[1] == ch and _norm(c[2]) in relevant_pages and c[0] in texts),
                    key=lambda c: _order(c[0]))
        if len(ok) < per_channel:
            raise RuntimeError(f"канал {ch}: релевантних статей з текстом {len(ok)}, а треба {per_channel}")
        chosen += ok[:per_channel]
    return chosen


async def build_reference_set(per_channel: int = PER_CHANNEL) -> dict[str, int]:
    """Зібрати набір один раз. Якщо він уже є — не чіпаємо: інакше прогони до і після не можна порівнювати."""
    with pg._connect() as c:
        c.execute(SCHEMA)
        existing = c.execute("SELECT count(*) FROM hc_articles").fetchone()
        if existing and existing[0]:
            raise RuntimeError("еталонний набір уже зібрано — не перезаписуємо (видалити hc_articles можна лише вручну)")
        candidates = c.execute(
            """SELECT DISTINCT ON (d.article_id) d.article_id, u.channel, d.notion_page_id
               FROM drafts d JOIN used_articles u USING (article_id)
               WHERE u.channel = ANY(%s) AND d.notion_page_id IS NOT NULL
               ORDER BY d.article_id, d.created_at""", (list(CHANNELS),)).fetchall()
    rows = await notion.query_rows(os.environ["NOTION_DATABASE_ID"],
                                   {"property": RELEVANCE_PROP, "select": {"equals": "yes"}})
    relevant = {_norm(p["id"]) for p in rows}
    texts = comms.fetch_by_ids([c[0] for c in candidates if _norm(c[2]) in relevant])
    chosen = pick_reference(candidates, relevant, texts, per_channel)
    with pg._connect() as c:
        for aid, ch, page in chosen:
            a = texts[aid]
            c.execute("""INSERT INTO hc_articles (article_id, channel, url, title, text, source, published_at,
                                                  notion_page_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                      (aid, ch, a.url, a.title, a.text, a.source, a.published_at, page))
    counts = {ch: sum(1 for c in chosen if c[1] == ch) for ch in CHANNELS}
    log.info("еталонний набір: %s (кандидатів %d, релевантних рядків у MVP %d)", counts, len(candidates), len(relevant))
    return counts


# ---------- сховище прогонів ----------

def init_db() -> None:
    with pg._connect() as c:
        c.execute(SCHEMA)
    hc_eval.init_db()  # оцінки й звіти посилаються на hc_runs і hc_articles, тож після них
    hc_report.init_db()
    with pg._connect() as c:
        # прогін, що йшов під час деплою/перезапуску, уже ніхто не допише сам — лише {"resume": run_id}
        c.execute("UPDATE hc_runs SET status = 'interrupted', finished_at = now() WHERE status = 'running'")


def reference_articles() -> list[RefArticle]:
    with pg._connect() as c:
        rows = c.execute("SELECT article_id, channel, url, title, text, source, published_at FROM hc_articles "
                         "ORDER BY channel, article_id").fetchall()
    return [RefArticle(Article(id=r[0], url=r[2], title=r[3], text=r[4], source=r[5], published_at=r[6]), r[1])
            for r in rows]


def done_articles(run_id: str) -> set[str]:
    """Статті, для яких у прогоні вже є результат (збій моделі не рахується — при resume пробуємо ще раз)."""
    with pg._connect() as c:
        return {r[0] for r in c.execute(
            "SELECT article_id FROM hc_drafts WHERE run_id = %s AND outcome <> 'error'", (run_id,))}


def create_run(run_id: str, trigger: str, model_version: str) -> None:
    with pg._connect() as c:
        c.execute("INSERT INTO hc_runs (run_id, trigger, model_version, status) VALUES (%s, %s, %s, 'running')",
                  (run_id, trigger, model_version))


def reopen_run(run_id: str) -> tuple[str, str]:
    """resume: прогін знову running; повертає (trigger, model_version), щоб догенерувати тією ж моделлю."""
    with pg._connect() as c:
        r = c.execute("UPDATE hc_runs SET status = 'running', finished_at = NULL, error = NULL WHERE run_id = %s "
                      "AND status <> 'running' RETURNING trigger, model_version", (run_id,)).fetchone()
    if not r:
        raise KeyError(run_id)
    return r[0], r[1]


def save_draft(run_id: str, article_id: str, outcome: str, draft: Draft | None, error: str | None,
               duration_ms: int) -> None:
    extraction = None
    if draft:
        facts = {"facts_used": draft.facts_used, "facts_rejected": draft.facts_rejected, **draft.review}
        extraction = json.dumps(facts, ensure_ascii=False) if any(facts.values()) else None
    with pg._connect() as c:
        c.execute(
            """INSERT INTO hc_drafts (run_id, article_id, outcome, lang, text, extraction, error, duration_ms)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
               ON CONFLICT (run_id, article_id) DO UPDATE SET outcome = excluded.outcome, lang = excluded.lang,
                 text = excluded.text, extraction = excluded.extraction, error = excluded.error,
                 duration_ms = excluded.duration_ms, created_at = now()""",
            (run_id, article_id, outcome, draft.lang if draft else None, draft.text if draft else None, extraction,
             error, duration_ms))


def finish_run(run_id: str, status: str, error: str | None = None) -> None:
    with pg._connect() as c:
        c.execute("UPDATE hc_runs SET status = %s, finished_at = now(), error = %s WHERE run_id = %s",
                  (status, error, run_id))


def list_runs(limit: int = 20) -> list[dict]:
    """Прогони з підсумком: скільки ok / needs_manual_review / refused / error."""
    with pg._connect() as c:
        rows = c.execute(
            """SELECT r.run_id, r.trigger, r.model_version, r.status, r.started_at, r.finished_at, r.error,
                      count(d.*) FILTER (WHERE d.outcome = 'ok'),
                      count(d.*) FILTER (WHERE d.outcome = 'needs_manual_review'),
                      count(d.*) FILTER (WHERE d.outcome = 'refused'),
                      count(d.*) FILTER (WHERE d.outcome = 'error')
               FROM hc_runs r LEFT JOIN hc_drafts d USING (run_id)
               GROUP BY r.run_id ORDER BY r.started_at DESC LIMIT %s""", (limit,)).fetchall()
    keys = ("run_id", "trigger", "model_version", "status", "started_at", "finished_at", "error",
            "ok", "needs_manual_review", "refused", "error_count")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def get_run(run_id: str) -> dict | None:
    runs = [r for r in list_runs(limit=1000) if r["run_id"] == run_id]
    if not runs:
        return None
    with pg._connect() as c:
        drafts = c.execute("""SELECT d.article_id, a.channel, a.title, d.outcome, d.error, d.duration_ms
                              FROM hc_drafts d JOIN hc_articles a USING (article_id)
                              WHERE d.run_id = %s ORDER BY a.channel, d.article_id""", (run_id,)).fetchall()
    keys = ("article_id", "channel", "title", "outcome", "error", "duration_ms")
    return {**runs[0], "drafts": [dict(zip(keys, d, strict=True)) for d in drafts]}


def last_run() -> dict | None:
    runs = list_runs(limit=1)
    return runs[0] if runs else None


# ---------- прогін ----------

def model_for(endpoint_id: str | None = None, mode: str | None = None) -> ModelClient:
    """Модель для прогону: за замовчуванням — та, що в кнопок (env); вручну можна вказати іншу версію."""
    endpoint = endpoint_id or os.environ.get("RUNPOD_ENDPOINT_ID")
    api_key = os.environ.get("RUNPOD_API_KEY")
    if not (endpoint and api_key):
        raise RuntimeError("RUNPOD_ENDPOINT_ID і RUNPOD_API_KEY не задані")
    return RunPodModelClient(endpoint, api_key, mode=mode or os.environ.get("RUNPOD_MODE", "draft_grounded"))


def start(trigger: str, model: ModelClient | None = None, resume: str | None = None) -> tuple[str, ModelClient]:
    """Зареєструвати прогін (новий або resume) і повернути (run_id, модель); сам прогін — run()."""
    global _current
    if _current:
        raise AlreadyRunning(_current)
    if resume:
        _, version = reopen_run(resume)
        if model is None:  # догенеровуємо тією ж моделлю, що й почали
            endpoint, _, mode = version.removeprefix("runpod-").partition(":")
            model = model_for(endpoint, mode or None)
        run_id = resume
    else:
        model = model or model_for()
        run_id = f"hc-{uuid.uuid4().hex[:10]}"
        create_run(run_id, trigger, model.version)
    _current = run_id
    return run_id, model


async def _wait_for_buttons() -> None:
    """Кнопки першими: генерацію починаємо, лише коли в їхньому пулі моделі лишається вільне місце."""
    while pipeline._in_pool + _active >= pipeline.MAX_PARALLEL_DRAFTS:
        await asyncio.sleep(WAIT_S)


async def _one(run_id: str, model: ModelClient, ref: RefArticle, sem: asyncio.Semaphore) -> str:
    global _active
    loop = asyncio.get_running_loop()
    async with sem:
        await _wait_for_buttons()
        _active += 1
        t0 = loop.time()
        draft: Draft | None = None
        error: str | None = None
        try:
            draft = await loop.run_in_executor(_POOL, model.draft, ref.article, pipeline.CHANNEL_LANG[ref.channel])
            outcome = "needs_manual_review" if draft.needs_review else "ok"
        except GroundingRefused as e:
            outcome, error = "refused", str(e)[:500]
        except Exception as e:
            outcome, error = "error", f"{type(e).__name__}: {e}"[:500]
        finally:
            _active -= 1
        save_draft(run_id, str(ref.article.id), outcome, draft, error, int((loop.time() - t0) * 1000))
        return outcome


async def run(run_id: str, model: ModelClient) -> None:
    """Драфт для кожної еталонної статті, якої ще немає в прогоні. Кожен результат пишеться одразу."""
    global _current
    try:
        done = done_articles(run_id)
        todo = [r for r in reference_articles() if str(r.article.id) not in done]
        log.info("health check %s: %s, статей %d (вже є %d)", run_id, model.version, len(todo), len(done))
        sem = asyncio.Semaphore(PARALLEL)
        # return_exceptions: якщо впаде запис однієї статті, решта генерацій усе одно дійде до кінця, а не лишиться
        # висіти після того, як прогін уже позначено failed
        results = await asyncio.gather(*(_one(run_id, model, r, sem) for r in todo), return_exceptions=True)
        if errors := [r for r in results if isinstance(r, BaseException)]:
            raise errors[0]
        outcomes = [str(r) for r in results]
        finish_run(run_id, "done")
        log.info("health check %s готово: %s", run_id, {o: outcomes.count(o) for o in set(outcomes)})
        if hc_eval.enabled():  # оцінка → порівняння → звіт → алерт; збій тут прогін уже не ламає
            from . import hc_flow  # локально: hc_flow читає прогони з цього модуля
            await hc_flow.after_run(run_id)
    except Exception as e:
        log.exception("health check %s failed", run_id)
        finish_run(run_id, "failed", f"{type(e).__name__}: {e}"[:500])
    finally:
        _current = None


def due(last: dict | None, now: datetime) -> str | None:
    """Що робити розкладу: "resume" — останній прогін обірвав деплой; "new" — він старший за EVERY_DAYS; None — нічого."""
    if last and last["status"] == "interrupted":
        return "resume"
    if last is None or (now - last["started_at"]).total_seconds() >= EVERY_DAYS * 86400:
        return "new"
    return None


async def schedule_loop() -> None:
    """Раз на годину перевіряє, чи пора запускати (лише якщо задано HEALTHCHECK_EVERY_DAYS)."""
    while True:
        try:
            last = last_run()
            action = None if _current else due(last, datetime.now(timezone.utc))
            if action:
                task = asyncio.create_task(run(*start("schedule", resume=last["run_id"] if action == "resume" and last
                                                      else None)))
                _tasks.add(task)
                task.add_done_callback(_tasks.discard)
        except Exception:
            log.exception("health check: розклад")
        await asyncio.sleep(SCHEDULE_CHECK_S)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.argv[1:] != ["build-set"]:
        sys.exit("usage: python -m app.healthcheck build-set")
    print(asyncio.run(build_reference_set()))
