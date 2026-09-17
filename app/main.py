"""Бекенд: приймає вебхук від Notion-кнопки, обирає статтю, пише пост, повертає його в таблицю MVP.

Потік:
  Notion (кнопка на сторінці «Сomms product») --POST /webhook--> цей сервер
      --> (1) articles.db: обрати статтю (заглушка LLM)
      --> (2) написати пост (заглушка LLM)
      --> (3) Notion API: створити рядок у таблиці MVP (Draft, Source, Status=Done, Score)
              або, якщо кнопка стояла в рядку таблиці, оновити цей рядок
      --> (4) posts.db: записати журнал генерації
"""
import logging
import os

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from . import db, llm, notion

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("post-generator")

DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").replace("-", "")
PROP_DRAFT = os.environ.get("NOTION_PROP_DRAFT", "Draft")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

app = FastAPI(title="Post generator (MLOps prototype)")


@app.on_event("startup")
def _startup():
    db.init_db()
    db.seed_articles_if_empty()
    log.info("DBs ready: %s, %s; target Notion DB=%s", db.ARTICLES_DB, db.POSTS_DB, DATABASE_ID)


@app.get("/health")
def health():
    return {"ok": True, "model": llm.MODEL_NAME, "articles": len(db.list_articles()),
            "notion_database_id": DATABASE_ID}


@app.get("/notion/check")
async def notion_check():
    """Діагностика: чи бачить інтеграція таблицю MVP і які там властивості."""
    d = await notion.get_database(DATABASE_ID)
    return {"title": "".join(t.get("plain_text", "") for t in d.get("title", [])),
            "properties": {k: v["type"] for k, v in d.get("properties", {}).items()}}


@app.get("/articles")
def articles():
    return [dict(a) for a in db.list_articles()]


@app.get("/posts")
def posts():
    return db.list_posts()


def _page_id_from_payload(payload: dict) -> str | None:
    """Notion-автоматизація «Send webhook» шле {"source": {...}, "data": {<page object>}}."""
    data = payload.get("data") or {}
    if isinstance(data, dict) and data.get("object") == "page":
        return data.get("id")
    # запасні варіанти для ручного тесту через curl
    return payload.get("page_id") or payload.get("id")


async def process(page_id: str) -> None:
    topic = None
    article = None
    target_row: str | None = None  # id рядка MVP, якщо кнопка була в рядку
    try:
        page = await notion.get_page(page_id)
        if notion.parent_database_id(page) == DATABASE_ID:
            # кнопка стоїть у рядку таблиці MVP → тема = поточний Draft, оновлюємо цей рядок
            target_row = page_id
            topic = notion.extract_text(page, PROP_DRAFT)
        else:
            # кнопка на рівні сторінки → теми немає, створюємо новий рядок
            topic = None

        article = llm.select_article(topic)
        post_text = llm.write_post(article, topic)
        headline = llm.write_headline(article, topic, n=db.count_posts() + 1)
        props = notion.build_properties(
            draft=headline,
            source=f"{article['title']} — {article['url']}",
            status="Done",
            score=llm.score_article(article, topic),
        )
        if target_row:
            row = await notion.update_row(target_row, props)
            await notion.append_body(target_row, post_text)
        else:
            row = await notion.create_row(DATABASE_ID, props, body=post_text)
        db.log_post(row["id"], article["id"], topic, post_text, llm.MODEL_NAME)
        log.info("OK row=%s article=%s", row["id"], article["id"])
    except Exception as e:
        log.exception("FAILED page=%s", page_id)
        db.log_post(page_id, article["id"] if article else 0, topic, "", llm.MODEL_NAME,
                    status="error", error=str(e))
        try:
            err_props = notion.build_properties(draft=f"ERROR: {e}"[:200], status="Not started")
            if target_row:
                await notion.update_row(target_row, err_props)
            else:
                await notion.create_row(DATABASE_ID, err_props)
        except Exception:
            log.exception("Could not write error back to Notion")


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    # Notion дозволяє додати кастомний заголовок у автоматизації «Send webhook»
    if WEBHOOK_SECRET and request.headers.get("x-webhook-secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    payload = await request.json()
    page_id = _page_id_from_payload(payload)
    if not page_id:
        raise HTTPException(status_code=400, detail="no page id in payload")
    log.info("webhook received page=%s", page_id)
    # Відповідаємо Notion одразу (він чекає < кількох секунд), роботу робимо у фоні
    background.add_task(process, page_id)
    return {"accepted": True, "page_id": page_id}
