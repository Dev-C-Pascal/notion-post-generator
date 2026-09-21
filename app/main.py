"""Бекенд: приймає тригер (кнопка Notion або POST /run), проганяє pipeline, повертає драфти в таблицю MVP.

Потік:
  Notion (кнопка) --POST /webhook--> сервер --> 202 {run_id} одразу, робота у фоні:
      fetch (articles.db) → relevance → extraction → draft → evaluate → write_notion (рядок у MVP)
      → runs.db: журнал прогону та драфтів
"""
import logging
import os

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import db, notion, pg, pipeline
from .llm import get_model_client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("post-generator")

DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").replace("-", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
RUN_LIMIT = int(os.environ.get("RUN_ARTICLES", "1"))  # скільки статей обробляти за один прогін

app = FastAPI(title="Post generator (MLOps prototype)")


@app.on_event("startup")
def _startup() -> None:
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
    if WEBHOOK_SECRET and request.headers.get("x-webhook-secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")


def _start_run(background: BackgroundTasks, topic: str | None = None) -> JSONResponse:
    run_id = pipeline.new_run_id()
    background.add_task(pipeline.run_pipeline, run_id, database_id=DATABASE_ID, limit=RUN_LIMIT, topic=topic)
    return JSONResponse(status_code=202, content={"run_id": run_id, "status": "running"})


@app.post("/run", status_code=202)
async def run(request: Request, background: BackgroundTasks) -> JSONResponse:
    """Ручний/cron-тригер. Тіло (необов'язково): {"topic": "..."}."""
    _check_secret(request)
    body = await request.json() if int(request.headers.get("content-length") or 0) else {}
    log.info("POST /run topic=%s", body.get("topic"))
    return _start_run(background, body.get("topic"))


@app.post("/webhook", status_code=202)
async def webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
    """Тригер з кнопки Notion (Send webhook). Payload: {"source": {...}, "data": {<page object>}}."""
    _check_secret(request)
    payload = await request.json()
    page = payload.get("data") or {}
    log.info("webhook from page=%s", page.get("id"))
    return _start_run(background)
