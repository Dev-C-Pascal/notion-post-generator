"""Бекенд: приймає тригер (кнопка Notion або POST /run), проганяє pipeline, повертає драфти в таблицю MVP.

Потік:
  Notion (кнопка) --POST /webhook--> сервер --> 202 {run_id} одразу, робота у фоні:
      select (топ дня Андрія) → draft (модель на RunPod) → write_notion (рядок у MVP) + postgen
      → runs.db: журнал прогону та драфтів
"""
import asyncio
import hmac
import logging
import os
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import db, hc_alert, hc_compare, hc_eval, hc_flow, hc_report, healthcheck, notion, pg, pipeline, status
from .llm import get_model_client

load_dotenv()
# логи й у файл на volume (/srv/data): stdout контейнера зникає при кожному деплої, а з ним і причини збоїв
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[
    logging.StreamHandler(),
    RotatingFileHandler(db.DATA_DIR / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"),
])
log = logging.getLogger("post-generator")

DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").replace("-", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
# порожній чи значення з .env.example: будь-хто, хто знайде адресу, запускав би генерацію за гроші RunPod
WEAK_SECRETS = {"", "change-me"}
RUN_LIMIT = int(os.environ.get("RUN_ARTICLES", "1"))  # скільки статей обробляти за один прогін

app = FastAPI(title="Post generator (MLOps prototype)")


@app.on_event("startup")
async def _startup() -> None:
    if WEBHOOK_SECRET in WEAK_SECRETS:
        raise RuntimeError("WEBHOOK_SECRET порожній або стандартний (change-me) — згенерувати: openssl rand -hex 24")
    db.init_db()
    if pg.enabled():
        pg.init_db()
        healthcheck.init_db()
        log.info("drafts Postgres ready")
        if healthcheck.EVERY_DAYS:
            app.state.healthcheck_schedule = asyncio.create_task(healthcheck.schedule_loop())
        if hc_eval.enabled():  # деплой міг обірвати оцінку чи звіт останнього прогону — дозавершити
            app.state.healthcheck_followup = asyncio.create_task(hc_flow.resume_followups())
    log.info("DBs ready: %s, %s; target Notion DB=%s", db.ARTICLES_DB, db.RUNS_DB, DATABASE_ID)
    status.restarted()  # у блоці статусу міг лишитись «генерується» від обірваних прогонів


@app.get("/health")
def health() -> dict:
    return {"ok": True, "model": get_model_client().version, "articles": db.count_articles(),
            "notion_database_id": DATABASE_ID}


@app.get("/notion/check")
async def notion_check() -> dict:
    d = await notion.get_database(DATABASE_ID)
    return {"title": "".join(t.get("plain_text", "") for t in d.get("title", [])),
            "properties": {k: v["type"] for k, v in d.get("properties", {}).items()}}


@app.get("/articles")
def articles() -> list:
    return [a.model_dump() for a in db.fetch_articles(limit=100, only_unprocessed=False)]


@app.get("/runs")
def runs() -> list:
    return db.list_runs()


@app.get("/runs/{run_id}")
def run_status(run_id: str) -> dict:
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="run not found")
    return r


def _check_secret(request: Request) -> None:
    given = request.headers.get("x-webhook-secret", "")
    if not hmac.compare_digest(given.encode(), WEBHOOK_SECRET.encode()):
        raise HTTPException(status_code=401, detail="bad secret")


def _start_run(background: BackgroundTasks, channel: str | None) -> JSONResponse:
    if channel and channel not in pipeline.CHANNEL_LANG:
        raise HTTPException(status_code=404, detail=f"unknown channel, expected one of {sorted(pipeline.CHANNEL_LANG)}")
    run_id = pipeline.new_run_id()
    background.add_task(pipeline.run_pipeline, run_id, database_id=DATABASE_ID, limit=RUN_LIMIT, channel=channel)
    return JSONResponse(status_code=202, content={"run_id": run_id, "status": "running", "channel": channel})


@app.post("/run", status_code=202)
async def run(request: Request, background: BackgroundTasks, channel: str | None = None) -> JSONResponse:
    """Ручний/cron-тригер. ?channel=fb|x — під який канал писати пост (без нього — мовою статті)."""
    _check_secret(request)
    log.info("POST /run channel=%s", channel)
    return _start_run(background, channel)


@app.post("/webhook", status_code=202)
@app.post("/webhook/{channel}", status_code=202)
async def webhook(request: Request, background: BackgroundTasks, channel: str | None = None) -> JSONResponse:
    """Тригер з кнопки Notion (Send webhook). Payload: {"source": {...}, "data": {<page object>}}.
    Кнопка на канал: /webhook/fb — український пост для FB, /webhook/x — англійський тред для X."""
    _check_secret(request)
    try:
        payload = await request.json()
    except ValueError:  # тіло не JSON — сторінка лише для журналу, прогону це не заважає
        payload = {}
    page = (payload.get("data") if isinstance(payload, dict) else None) or {}
    log.info("webhook from page=%s channel=%s", page.get("id"), channel)
    return _start_run(background, channel)


# Health Check — лише з сервера (Caddy назовні не пускає): curl 127.0.0.1:8000/health-check …
@app.post("/health-check", status_code=202)
async def health_check_start(request: Request, background: BackgroundTasks) -> JSONResponse:
    """Прогін еталонного набору. Тіло (необов'язково): {"endpoint_id": "...", "mode": "..."} — інша версія моделі;
    {"resume": "<run_id>"} — догенерувати обірваний прогін тією ж моделлю."""
    _check_secret(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    try:
        model = None if body.get("resume") else healthcheck.model_for(body.get("endpoint_id"), body.get("mode"))
        run_id, model = healthcheck.start("manual", model, resume=body.get("resume"))
    except healthcheck.AlreadyRunning as e:
        raise HTTPException(status_code=409, detail=f"health check {e} ще йде") from e
    except KeyError as e:
        raise HTTPException(status_code=404, detail="run not found or still running") from e
    log.info("health check %s: %s", run_id, model.version)
    background.add_task(healthcheck.run, run_id, model)
    return JSONResponse(status_code=202, content={"run_id": run_id, "model_version": model.version})


@app.get("/health-check/runs")
def health_check_runs() -> list:
    return healthcheck.list_runs()


@app.get("/health-check/runs/{run_id}")
def health_check_run(run_id: str) -> dict:
    r = healthcheck.get_run(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="run not found")
    return r


@app.post("/health-check/runs/{run_id}/evaluate", status_code=202)
async def health_check_evaluate(run_id: str, request: Request, background: BackgroundTasks) -> JSONResponse:
    """Оцінити драфти прогону агентом-оцінювачем (інструкція Антона, дешева модель Anthropic). Тіло (необов'язково):
    {"force": true} — оцінити заново, {"judge_model": "..."} — іншою моделлю; оцінки різних суддів лежать окремо."""
    _check_secret(request)
    if not healthcheck.get_run(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    try:
        judge = hc_eval.get_judge(body.get("judge_model"))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if hc_flow.is_busy(run_id):
        raise HTTPException(status_code=409, detail="оцінка цього прогону вже йде")
    background.add_task(hc_flow.after_run, run_id, judge=judge, force=bool(body.get("force")))
    return JSONResponse(status_code=202, content={"run_id": run_id, "judge": judge.key})


@app.get("/health-check/runs/{run_id}/evals")
def health_check_evals(run_id: str, judge: str | None = None) -> list:
    """Оцінки по статтях (без сирих відповідей судді); ?judge=<ключ> — лише одного судді."""
    if not healthcheck.get_run(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    return hc_eval.list_evals(run_id, judge)


@app.get("/health-check/compare")
async def health_check_compare(run: str, base: str | None = None, judge: str | None = None) -> dict:
    """Порівняння прогону `run` з `base` (за замовчуванням — попередній завершений) оцінками одного судді (`judge`,
    за замовчуванням — поточний). Нічого не оцінює і не коштує грошей: що не оцінено, те в warnings."""
    for rid in (run, base):
        if rid and not hc_compare.run_info(rid):
            raise HTTPException(status_code=404, detail=f"run {rid} not found")
    c = await hc_flow.compare_runs(run, base, judge)
    if c is None:
        return {"run_id": run, "base_run": None, "note": "це перший завершений прогін: порівнювати нема з чим"}
    return c.as_dict()


@app.get("/health-check/runs/{run_id}/report")
async def health_check_report(run_id: str, base: str | None = None, judge: str | None = None,
                              fresh: bool = False) -> Response:
    """Звіт по прогону (markdown). Без параметрів — збережений ланцюжком після прогону; якщо його ще нема, а також з
    ?fresh=true чи ?base=<run_id> — збирається на льоту: нічого не оцінює, не зберігає і не публікує."""
    if not hc_compare.run_info(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    stored = None if fresh or base else hc_report.get_report(run_id, judge)
    markdown = stored["markdown"] if stored else await hc_flow.make_report(run_id, base, judge)
    return Response(content=markdown, media_type="text/markdown; charset=utf-8")


@app.post("/health-check/runs/{run_id}/report", status_code=202)
async def health_check_report_rebuild(run_id: str, request: Request, background: BackgroundTasks,
                                      republish: bool = False) -> JSONResponse:
    """Зібрати звіт наново й зберегти. Дооцінює те, чого бракує, тож може йти хвилини: результат — у GET цього ж шляху.
    Сторінку в Notion (NOTION_HEALTHCHECK_PAGE_ID) створює лише якщо її для цього прогону ще нема; ?republish=true —
    навмисно ще одну (стару бекенд не чіпає)."""
    _check_secret(request)
    if not hc_compare.run_info(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    if hc_flow.is_busy(run_id):
        raise HTTPException(status_code=409, detail="кроки після цього прогону вже йдуть")
    background.add_task(hc_flow.rebuild_report, run_id, republish)
    return JSONResponse(status_code=202, content={"run_id": run_id, "report": f"/health-check/runs/{run_id}/report"})


@app.post("/health-check/alert-test")
async def health_check_alert_test(request: Request) -> dict:
    """Перевірити, що алерти доходять: надсилає тестове повідомлення в налаштовані канали (див. .env.example)."""
    _check_secret(request)
    sent = await hc_alert.send("🧪 Тест Health Check: якщо ви бачите це повідомлення, алерти доходять.", level="test")
    return {"channels": hc_alert.channels(), "sent": sent}
