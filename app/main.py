"""Бекенд: приймає тригер (кнопка Notion або POST /run), проганяє pipeline, повертає драфти в таблицю MVP.

Потік:
  Notion (кнопка) --POST /webhook--> сервер --> 202 {run_id} одразу, робота у фоні:
      select (топ дня Андрія) → draft (модель на RunPod) → write_notion (рядок у MVP) + postgen
      → runs.db: журнал прогону та драфтів
"""
import hmac
import logging
import os
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import db, notion, pg, pipeline
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
def _startup() -> None:
    if WEBHOOK_SECRET in WEAK_SECRETS:
        raise RuntimeError("WEBHOOK_SECRET порожній або стандартний (change-me) — згенерувати: openssl rand -hex 24")
    db.init_db()
    if pg.enabled():
        pg.init_db()
        log.info("drafts Postgres ready")
    log.info("DBs ready: %s, %s; target Notion DB=%s", db.ARTICLES_DB, db.RUNS_DB, DATABASE_ID)


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
