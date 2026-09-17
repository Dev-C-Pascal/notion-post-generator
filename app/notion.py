"""Мінімальний клієнт Notion REST API: прочитати сторінку, створити рядок у таблиці, оновити рядок.

Авторизація: заголовок `Authorization: Bearer <NOTION_TOKEN>` (internal integration).
Інтеграція бачить лише ті сторінки/таблиці, до яких її підключили через «...» → Connections.
"""
import os

import httpx

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TEXT_LIMIT = 2000  # ліміт Notion на один текстовий фрагмент у title/rich_text


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


def _rich(text: str) -> list:
    """Розбити довгий текст на фрагменти по 2000 символів (вимога Notion)."""
    return [{"text": {"content": text[i:i + TEXT_LIMIT]}} for i in range(0, max(len(text), 1), TEXT_LIMIT)]


async def get_page(page_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        return _check(await client.get(f"{NOTION_API}/pages/{page_id}", headers=_headers()))


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


def build_properties(*, draft: str, source: str | None = None, status: str | None = None,
                     score: float | None = None) -> dict:
    """Властивості під схему таблиці MVP: Draft(title), Source(text), Status(select), Score(number)."""
    props: dict = {os.environ.get("NOTION_PROP_DRAFT", "Draft"): {"title": _rich(draft)}}
    if source is not None:
        props[os.environ.get("NOTION_PROP_SOURCE", "Source")] = {"rich_text": _rich(source)}
    if status is not None:
        props[os.environ.get("NOTION_PROP_STATUS", "Status")] = {"select": {"name": status}}
    if score is not None:
        props[os.environ.get("NOTION_PROP_SCORE", "Score")] = {"number": score}
    return props


def _paragraphs(text: str) -> list:
    """Тіло сторінки: один paragraph-блок на абзац (кожен ≤ 2000 символів)."""
    return [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich(par)}}
            for par in text.split("\n\n") if par.strip()]


async def create_row(database_id: str, properties: dict, body: str | None = None) -> dict:
    """POST /v1/pages — новий рядок у таблиці; body (якщо є) стає контентом сторінки рядка."""
    payload: dict = {"parent": {"database_id": database_id}, "properties": properties}
    if body:
        payload["children"] = _paragraphs(body)
    async with httpx.AsyncClient(timeout=30) as client:
        return _check(await client.post(f"{NOTION_API}/pages", headers=_headers(), json=payload))


async def append_body(page_id: str, body: str) -> dict:
    """PATCH /v1/blocks/{id}/children — дописати текст у контент існуючого рядка."""
    async with httpx.AsyncClient(timeout=30) as client:
        return _check(await client.patch(f"{NOTION_API}/blocks/{page_id}/children", headers=_headers(),
                                         json={"children": _paragraphs(body)}))


async def update_row(page_id: str, properties: dict) -> dict:
    """PATCH /v1/pages/{id} — оновити властивості існуючого рядка."""
    async with httpx.AsyncClient(timeout=30) as client:
        return _check(await client.patch(
            f"{NOTION_API}/pages/{page_id}", headers=_headers(), json={"properties": properties},
        ))


async def get_database(database_id: str) -> dict:
    """GET /v1/databases/{id} — схема таблиці (для діагностики)."""
    async with httpx.AsyncClient(timeout=30) as client:
        return _check(await client.get(f"{NOTION_API}/databases/{database_id}", headers=_headers()))
