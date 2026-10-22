"""Журнал без секретів. Токен бота Telegram стоїть у самій адресі запиту (api.telegram.org/bot<токен>/sendMessage), а httpx
пише адресу кожного запиту в журнал на рівні INFO: без цього токен лежав би відкритим текстом у data/app.log на сервері й у
stdout контейнера. Те саме з адресою вебхука (у Slack і Discord секрет — частина шляху).

install() вішає на всі обробники кореневого логера фільтр, який перед записом замінює:
  • значення змінних середовища, чиї імена містять TOKEN, SECRET, PASSWORD, API_KEY або WEBHOOK (коротші за MIN_SECRET_LEN
    символів не чіпаємо: заміна на кшталт «t» зіпсувала б кожен рядок);
  • токен в адресі Telegram, навіть якщо його немає в середовищі (бот міг бути підставлений іншим шляхом);
  • пароль в адресі підключення (postgresql://user:пароль@host/db).
Повідомлення й traceback чиститься до того, як рядок потрапить у будь-який обробник.
"""
from __future__ import annotations

import logging
import os
import re

MASK = "<приховано>"
MIN_SECRET_LEN = 8
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|API_KEY|WEBHOOK", re.I)
_TELEGRAM = re.compile(r"(api\.telegram\.org/(?:file/)?bot)[^/\s'\"?]+", re.I)
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^/\s@]+(@)", re.I)


def secrets() -> list[str]:
    """Значення секретних змінних середовища, довші першими: адреса вебхука раніше за свої частини."""
    found = {v for k, v in os.environ.items() if _SECRET_NAME.search(k) and len(v) >= MIN_SECRET_LEN}
    return sorted(found, key=len, reverse=True)


def redact(text: str) -> str:
    text = _TELEGRAM.sub(lambda m: m.group(1) + MASK, text)
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + MASK + m.group(2), text)
    for secret in secrets():
        text = text.replace(secret, MASK)
    return text


class RedactFilter(logging.Filter):
    """Замінює секрети в повідомленні, traceback і stack_info запису. Ідемпотентний: кілька обробників — не проблема."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = redact(record.getMessage()), None
        except Exception:  # криве форматування (%s без аргументу тощо): хай його покаже сам logging, але без секретів
            record.msg = redact(str(record.msg))
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install(logger: logging.Logger | None = None) -> None:
    """Фільтр на кожен обробник (не на логер: фільтр логера не діє на записи його нащадків — httpx, anthropic…)."""
    for handler in (logger or logging.getLogger()).handlers:
        if not any(isinstance(f, RedactFilter) for f in handler.filters):
            handler.addFilter(RedactFilter())
