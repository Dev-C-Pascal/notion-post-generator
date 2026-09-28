"""Мінімальний клієнт Notion REST API: прочитати сторінку, створити рядок у таблиці, оновити рядок.

Авторизація: заголовок `Authorization: Bearer <NOTION_TOKEN>` (internal integration).
Інтеграція бачить лише ті сторінки/таблиці, до яких її підключили через «...» → Connections.
"""
import asyncio
import logging
import os

import httpx

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TEXT_LIMIT = 2000  # ліміт Notion на один текстовий фрагмент у title/rich_text
BLOCKS_LIMIT = 100  # ліміт Notion на кількість блоків в одному запиті
ATTEMPTS = 4  # запит до Notion пробуємо до 4 разів: пауза 2, 4, 8 с (або скільки скаже Retry-After)
BACKOFF_S = 2.0

log = logging.getLogger("notion")


def _headers() -> dict:
    token = os.environ.get("NOTION_TOKEN")
    if not token or token == "secret_xxx":
        raise RuntimeError("NOTION_TOKEN не заданий у .env")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _check(r: httpx.Response) -> dict:
    if r.status_code >= 400:
        raise RuntimeError(f"Notion {r.status_code}: {r.text}")
    return r.json()


async def _send(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
    """Запит до Notion з повтором лише там, де він точно не виконався: не вдалося з'єднатись або 429 (rate limit).
    ReadTimeout і 5xx не повторюємо — рядок міг уже створитись, повтор дав би дубль."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = await client.request(method, url, headers=_headers(), **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            if attempt == ATTEMPTS:
                raise
            wait = BACKOFF_S * 2 ** (attempt - 1)
            log.warning("Notion %s %s: %s, повтор %d/%d через %.0f с", method, url, type(e).__name__, attempt,
                        ATTEMPTS - 1, wait)
        else:
            if r.status_code != 429 or attempt == ATTEMPTS:
                return _check(r)
            wait = float(r.headers.get("Retry-After") or BACKOFF_S * 2 ** (attempt - 1))
            log.warning("Notion %s %s: 429, повтор %d/%d через %.0f с", method, url, attempt, ATTEMPTS - 1, wait)
        await asyncio.sleep(wait)
    raise AssertionError("unreachable")


def _rich(text: str) -> list:
    """Розбити довгий текст на фрагменти по 2000 символів (вимога Notion)."""
    return [{"text": {"content": text[i:i + TEXT_LIMIT]}} for i in range(0, max(len(text), 1), TEXT_LIMIT)]


async def get_page(page_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        return await _send(client, "GET", f"{NOTION_API}/pages/{page_id}")


def parent_database_id(page: dict) -> str | None:
    """Якщо сторінка — рядок таблиці, повертає id цієї таблиці (без дефісів)."""
    parent = page.get("parent") or {}
    db_id = parent.get("database_id")
    return db_id.replace("-", "") if db_id else None


def extract_text(page: dict, prop_name: str) -> str | None:
    """Дістати plain text із title/rich_text властивості."""
    prop = page.get("properties", {}).get(prop_name) or {}
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts) or None


def build_properties(*, draft: str, source: str | None = None, status: str | None = None) -> dict:
    """Властивості під схему таблиці MVP: Draft(title), Source(text), Status(select).
    Score бекенд не пише: оцінку ставить окрема система разом з людиною."""
    props: dict = {os.environ.get("NOTION_PROP_DRAFT", "Draft"): {"title": _rich(draft)}}
    if source is not None:
        props[os.environ.get("NOTION_PROP_SOURCE", "Source")] = {"rich_text": _rich(source)}
    if status is not None:
        props[os.environ.get("NOTION_PROP_STATUS", "Status")] = {"select": {"name": status}}
    return props


def _paragraphs(text: str) -> list:
    """Тіло сторінки: один paragraph-блок на абзац (кожен ≤ 2000 символів)."""
    return [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich(par)}}
            for par in text.split("\n\n") if par.strip()]


async def create_row(database_id: str, properties: dict, body: str | None = None) -> dict:
    """POST /v1/pages — новий рядок у таблиці; body (якщо є) стає контентом сторінки рядка."""
    payload: dict = {"parent": {"database_id": database_id}, "properties": properties}
    blocks = _paragraphs(body) if body else []
    if blocks:
        payload["children"] = blocks[:BLOCKS_LIMIT]
    async with httpx.AsyncClient(timeout=30) as client:
        page = await _send(client, "POST", f"{NOTION_API}/pages", json=payload)
        for i in range(BLOCKS_LIMIT, len(blocks), BLOCKS_LIMIT):
            await _send(client, "PATCH", f"{NOTION_API}/blocks/{page['id']}/children",
                        json={"children": blocks[i:i + BLOCKS_LIMIT]})
    return page


async def append_body(page_id: str, body: str) -> dict:
    """PATCH /v1/blocks/{id}/children — дописати текст у контент існуючого рядка."""
    async with httpx.AsyncClient(timeout=30) as client:
        return await _send(client, "PATCH", f"{NOTION_API}/blocks/{page_id}/children",
                           json={"children": _paragraphs(body)})


async def update_block(block_id: str, payload: dict) -> dict:
    """PATCH /v1/blocks/{id} — переписати вміст блоку (напр. {"callout": {"rich_text": [...]}})."""
    async with httpx.AsyncClient(timeout=30) as client:
        return await _send(client, "PATCH", f"{NOTION_API}/blocks/{block_id}", json=payload)


async def update_row(page_id: str, properties: dict) -> dict:
    """PATCH /v1/pages/{id} — оновити властивості існуючого рядка."""
    async with httpx.AsyncClient(timeout=30) as client:
        return await _send(client, "PATCH", f"{NOTION_API}/pages/{page_id}", json={"properties": properties})


async def get_database(database_id: str) -> dict:
    """GET /v1/databases/{id} — схема таблиці (для діагностики)."""
    async with httpx.AsyncClient(timeout=30) as client:
        return await _send(client, "GET", f"{NOTION_API}/databases/{database_id}")


async def query_edited_since(database_id: str, since: str) -> list[dict]:
    """POST /v1/databases/{id}/query — рядки, змінені з `since` (ISO), від старих до нових; усі сторінки курсора."""
    body: dict = {
        "filter": {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since}},
        "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}],
        "page_size": 100,
    }
    pages: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            r = await _send(client, "POST", f"{NOTION_API}/databases/{database_id}/query", json=body)
            pages += r["results"]
            if not r.get("has_more"):
                return pages
            body["start_cursor"] = r["next_cursor"]


async def read_body(page_id: str) -> list[str]:
    """Текст сторінки: по рядку на блок верхнього рівня (paragraph, heading, list item, quote…)."""
    blocks: list[str] = []
    params: dict = {"page_size": 100}
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            r = await _send(client, "GET", f"{NOTION_API}/blocks/{page_id}/children", params=params)
            for b in r["results"]:
                rich = (b.get(b.get("type", "")) or {}).get("rich_text")
                if rich is not None:
                    blocks.append("".join(t.get("plain_text", "") for t in rich))
            if not r.get("has_more"):
                return blocks
            params["start_cursor"] = r["next_cursor"]


def select_value(page: dict, prop_name: str) -> str | None:
    sel = (page.get("properties", {}).get(prop_name) or {}).get("select")
    return sel.get("name") if sel else None
