"""Куди бекенд шле повідомлення про Health Check: бот Telegram і/або вебхук (Slack, Mattermost, Discord). Журнал є завжди.

Telegram: HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN (від @BotFather) + HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID (число; у групи
від'ємне -100…, або @username каналу) і, якщо в групі увімкнено теми, HEALTHCHECK_ALERT_TELEGRAM_THREAD_ID. Бот має бути
в чаті: у групі — учасником, у каналі — адміном з правом публікації, в особистому діалозі людина спершу натискає /start.
Вебхук: HEALTHCHECK_ALERT_WEBHOOK, JSON із полями text і content.

Токен стоїть в адресі запиту до Telegram, тому помилки тут описуємо лише типом і відповіддю Telegram (description), а не
текстом винятку: httpx кладе в нього адресу з токеном. Загалом секрети з журналу прибирає й logsafe.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from . import logsafe

log = logging.getLogger("notify")

TELEGRAM_LIMIT = 4096  # символів у одному повідомленні


def channels() -> list[str]:
    """Куди бекенд вміє слати (журнал є завжди)."""
    found = []
    if os.environ.get("HEALTHCHECK_ALERT_WEBHOOK"):
        found.append("webhook")
    if os.environ.get("HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN") and os.environ.get("HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID"):
        found.append("telegram")
    return found


def _why(r: httpx.Response, channel: str) -> str:
    """Коротка причина відмови: що відповів Telegram («chat not found», «Unauthorized»), а для вебхука — код."""
    if channel == "telegram":
        try:
            body = r.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("description"):
            return logsafe.redact(f"Telegram: {body['description']} ({r.status_code})")[:300]
    return f"HTTP {r.status_code}"


async def _post(client: httpx.AsyncClient, channel: str, text: str, extra: dict[str, Any]) -> httpx.Response:
    if channel == "webhook":
        return await client.post(os.environ["HEALTHCHECK_ALERT_WEBHOOK"], json={"text": text, "content": text, **extra})
    token = os.environ["HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN"]
    body: dict[str, Any] = {"chat_id": os.environ["HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID"], "text": text[:TELEGRAM_LIMIT],
                            "disable_web_page_preview": True}
    thread = os.environ.get("HEALTHCHECK_ALERT_TELEGRAM_THREAD_ID", "").strip()
    if thread.isdigit():
        body["message_thread_id"] = int(thread)
    return await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=body)


async def send_detailed(text: str, **extra: Any) -> dict[str, dict[str, Any]]:
    """Надіслати текст у всі налаштовані канали: {канал: {"ok": bool, "error": str | None}}. Збій одного каналу не
    заважає іншим, а його причина — без адреси й токена — доходить до того, хто налаштовує (POST /health-check/alert-test)."""
    results: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for name in channels():
            try:
                r = await _post(client, name, text, extra)
                ok = r.status_code < 400
                results[name] = {"ok": ok, "error": None if ok else _why(r, name)}
            except Exception as e:  # у тексті винятку може бути адреса вебхука чи токен — лишається тільки тип
                results[name] = {"ok": False, "error": type(e).__name__}
            if not results[name]["ok"]:
                log.warning("повідомлення: канал %s не відповів (%s)", name, results[name]["error"])
    return results


async def send(text: str, **extra: Any) -> dict[str, bool]:
    """Те саме, що send_detailed, але лише {канал: чи дійшло}."""
    return {name: r["ok"] for name, r in (await send_detailed(text, **extra)).items()}
