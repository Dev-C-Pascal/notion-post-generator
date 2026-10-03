"""Рядок статусу під кнопками на сторінці «Сomms product»: що бекенд робить з кожним натисканням.

Кнопка Notion (Send webhook) не показує відповідь сервера, тож тестувальники тиснули по 5–8 разів поспіль.
Тепер кожне натискання одразу з'являється під кнопками й оновлюється:
шукаю статтю → у черзі → генерується → готово (посилання на рядок) / не вдалося / немає статей.

Блок — callout NOTION_STATUS_BLOCK_ID; не задано — модуль нічого не робить (локально, у тестах).
У callout видно лише коротку підказку; рядки натискань — у згорнутому toggle всередині нього
(03.10: довгий список під кнопками виглядав погано). Toggle з одним paragraph бекенд створює сам,
якщо його немає (перший запуск або хтось видалив), і далі переписує лише цей paragraph.
Стан живе в пам'яті процесу; після перезапуску блок каже, що прогони обірвано.
Збій оновлення блоку лише пишеться в лог — прогін через це не падає.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from . import notion

log = logging.getLogger("status")

KYIV = ZoneInfo("Europe/Kyiv")
MAX_LINES = 10  # скільки останніх натискань показувати (29.09: при 5 люди не бачили свого натискання й тиснули ще)
TITLE_CHARS = 60
HEADER = ("Генерація: зазвичай драфт з'являється в таблиці за 3–10 хв. "
          "Кожне натискання — окремий драфт: якщо ваше вже є в статусі нижче, тиснути повторно не треба.")
TOGGLE_TITLE = "Статус останніх натискань (оновлюється сам, новіші зверху)"
CHANNEL_LABEL = {"fb": "FB", "x": "X"}

_lines: dict[str, list[dict]] = {}  # run_id → rich_text рядка; порядок вставки = порядок натискань
_tasks: set[asyncio.Task] = set()
_lock: asyncio.Lock | None = None
_pending = False  # оновлення блоку вже чекає в черзі — воно й намалює найсвіжіший стан (ліміт Notion ~3 запити/с)
_body_id: str | None = None  # paragraph у toggle, куди пишуться рядки; None — знайти або створити заново


def enabled() -> bool:
    return bool(os.environ.get("NOTION_STATUS_BLOCK_ID"))


def now() -> str:
    return datetime.now(KYIV).strftime("%H:%M")


def page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def _seg(text: str, url: str | None = None) -> dict:
    t: dict = {"content": text}
    if url:
        t["link"] = {"url": url}
    return {"type": "text", "text": t}


def report(run_id: str, channel: str | None, started: str, state: str, *, title: str | None = None,
           url: str | None = None, note: str | None = None) -> None:
    """Оновити рядок прогону: «16:12 FB — генерується: «назва» (примітка)». Лише з потоку event loop
    (з потоку моделі — через loop.call_soon_threadsafe)."""
    if not enabled():
        return
    segs = [_seg(f"{started} {CHANNEL_LABEL.get(channel or '', 'авто')} — {state}")]
    if title:
        short = title if len(title) <= TITLE_CHARS else title[:TITLE_CHARS - 1] + "…"
        segs += [_seg(": "), _seg(f"«{short}»", url)]
    if note:
        segs.append(_seg(f" {note}"))
    _lines[run_id] = segs
    while len(_lines) > MAX_LINES:
        del _lines[next(iter(_lines))]
    _schedule()


def restarted() -> None:
    """Після старту бекенду: у пам'яті прогонів немає, а в блоці міг лишитись «генерується» — пояснюємо."""
    if not enabled():
        return
    _lines.clear()
    _lines["restart"] = [_seg(f"{now()} — бекенд перезапущено: прогони, що йшли, обірвано. "
                              "Якщо чекали на драфт і його немає в таблиці — натисніть кнопку ще раз.")]
    _schedule()


def _schedule() -> None:
    global _pending
    if _pending:
        return
    _pending = True
    task = asyncio.get_running_loop().create_task(_render())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _ensure_body(callout_id: str) -> str:
    """Знайти paragraph у toggle всередині callout; немає — створити. Заодно ставить коротку підказку в callout."""
    global _body_id
    if _body_id:
        return _body_id
    await notion.update_block(callout_id, {"callout": {"rich_text": [_seg(HEADER)]}})
    toggle = next((b for b in await notion.list_children(callout_id) if b.get("type") == "toggle"), None)
    if toggle is None:
        [toggle] = await notion.append_children(callout_id, [{"object": "block", "type": "toggle", "toggle": {
            "rich_text": [_seg(TOGGLE_TITLE)],
            "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}]}}])
    body = next((b for b in await notion.list_children(toggle["id"]) if b.get("type") == "paragraph"), None)
    if body is None:
        [body] = await notion.append_children(toggle["id"], [
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}])
    _body_id = body["id"]
    return _body_id


async def _render() -> None:
    """Переписати рядки поточним станом. Під замком: пізніший рендер завжди показує свіжіший стан."""
    global _lock, _pending, _body_id
    _lock = _lock or asyncio.Lock()
    async with _lock:
        _pending = False  # стан знімаємо зараз; зміни після цього моменту поставлять новий рендер
        rich: list[dict] = []
        for i, segs in enumerate(reversed(_lines.values())):
            rich += ([_seg("\n")] if i else []) + segs
        for attempt in (1, 2):  # друга спроба — якщо блок видалили руками: створюємо toggle заново
            try:
                body = await _ensure_body(os.environ["NOTION_STATUS_BLOCK_ID"])
                await notion.update_block(body, {"paragraph": {"rich_text": rich}})
                return
            except Exception:
                _body_id = None
                if attempt == 2:
                    log.warning("не вдалося оновити рядок статусу", exc_info=True)


async def drain() -> None:
    """Дочекатися всіх оновлень блоку (тести)."""
    while _tasks:
        await asyncio.gather(*list(_tasks))
