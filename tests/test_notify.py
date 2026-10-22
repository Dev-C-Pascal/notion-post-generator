"""Сповіщення Health Check: канали, причина відмови без токена, тема Telegram, ліміт довжини і — головне — токен бота й
адреса вебхука не потрапляють у журнал."""
import io
import logging
import sys

import httpx
import pytest

from app import logsafe, notify

TOKEN = "123456789:AAH-secret_token_value_xyz"
HOOK = "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXX"
SEND_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


@pytest.fixture
def telegram(monkeypatch):
    for name in ("HEALTHCHECK_ALERT_WEBHOOK", "HEALTHCHECK_ALERT_TELEGRAM_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID", "-1001234567890")


def test_channels_follow_the_environment(monkeypatch):
    for name in ("HEALTHCHECK_ALERT_WEBHOOK", "HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", "HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert notify.channels() == []
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", HOOK)
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", TOKEN)
    assert notify.channels() == ["webhook"]  # Telegram без chat_id не вважається налаштованим
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID", "-100")
    assert notify.channels() == ["webhook", "telegram"]


async def test_send_reaches_every_channel_and_one_failure_does_not_stop_the_other(monkeypatch, outbox, caplog):
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", HOOK)
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_CHAT_ID", "-100")
    monkeypatch.delenv("HEALTHCHECK_ALERT_TELEGRAM_THREAD_ID", raising=False)
    assert await notify.send("привіт", verdict="failed") == {"webhook": True, "telegram": True}
    (url1, body1), (url2, body2) = outbox["requests"]
    assert url1 == HOOK and body1 == {"text": "привіт", "content": "привіт", "verdict": "failed"}
    assert url2 == SEND_URL and body2 == {"chat_id": "-100", "text": "привіт", "disable_web_page_preview": True}
    outbox.update(status=500, requests=[])
    with caplog.at_level(logging.WARNING):
        assert await notify.send("ще раз") == {"webhook": False, "telegram": False}
    assert len(outbox["requests"]) == 2  # обидва канали спробувано
    assert "webhook" in caplog.text and "HTTP 500" in caplog.text
    assert HOOK not in caplog.text and TOKEN not in caplog.text  # адреса й токен у журнал не потрапляють


async def test_telegram_says_why_it_did_not_deliver(telegram, outbox):
    outbox.update(status=400, body={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
    result = await notify.send_detailed("x")
    assert result == {"telegram": {"ok": False, "error": "Telegram: Bad Request: chat not found (400)"}}
    outbox.update(status=401, body={"ok": False, "error_code": 401, "description": "Unauthorized"})
    assert (await notify.send_detailed("x"))["telegram"]["error"] == "Telegram: Unauthorized (401)"
    outbox.update(status=502, body=None)  # не JSON від Telegram — лише код
    assert (await notify.send_detailed("x"))["telegram"]["error"] == "HTTP 502"


async def test_a_network_error_is_reported_by_type_only(telegram, monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")  # у тексті винятку — адреса з токеном

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(boom), **kw))
    result = await notify.send_detailed("x")
    assert result == {"telegram": {"ok": False, "error": "ConnectError"}}


async def test_topic_of_a_forum_group_and_the_length_limit(telegram, monkeypatch, outbox):
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_THREAD_ID", "7")
    await notify.send("я" * 5000)
    body = outbox["requests"][0][1]
    assert body["message_thread_id"] == 7 and len(body["text"]) == notify.TELEGRAM_LIMIT
    monkeypatch.setenv("HEALTHCHECK_ALERT_TELEGRAM_THREAD_ID", "not-a-number")  # криве значення не ламає відправку
    outbox["requests"].clear()
    assert await notify.send("ok") == {"telegram": True} and "message_thread_id" not in outbox["requests"][0][1]


# ---------- секрети в журналі ----------

def test_redact_hides_tokens_hooks_keys_and_passwords(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_ALERT_WEBHOOK", HOOK)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-abcdefghijkl")
    monkeypatch.setenv("WEBHOOK_SECRET", "t")  # занадто коротке: заміна зіпсувала б кожен рядок
    text = (f'HTTP Request: POST {SEND_URL} "HTTP/1.1 200 OK"; hook {HOOK}; key sk-ant-api03-abcdefghijkl; '
            "db postgresql://postgen:s3cretpw@db:5432/postgen; t")
    out = logsafe.redact(text)
    for leaked in (TOKEN, HOOK, "sk-ant-api03-abcdefghijkl", "s3cretpw", "XXXXXXXXXXXXXXXX"):
        assert leaked not in out
    assert "api.telegram.org/bot<приховано>/sendMessage" in out and "postgresql://postgen:<приховано>@db:5432" in out
    assert out.endswith("; t")  # коротке значення лишилось як було


async def test_httpx_logs_the_telegram_url_and_the_filter_removes_the_token(telegram, monkeypatch):
    """httpx пише адресу кожного запиту на рівні INFO — а токен бота стоїть у ній. Без фільтра він лягав би в app.log."""
    root = logging.getLogger()
    raw, safe = io.StringIO(), io.StringIO()
    raw_handler, safe_handler = logging.StreamHandler(raw), logging.StreamHandler(safe)
    safe_handler.addFilter(logsafe.RedactFilter())
    old_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(raw_handler)
    root.addHandler(safe_handler)
    try:
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(200)), **kw))
        await notify.send("hello")
    finally:
        root.removeHandler(raw_handler)
        root.removeHandler(safe_handler)
        root.setLevel(old_level)
    assert TOKEN in raw.getvalue()  # доказ, що витік справжній: без фільтра токен у журналі
    assert "api.telegram.org" in safe.getvalue() and TOKEN not in safe.getvalue()


def test_the_filter_cleans_tracebacks_and_bad_formatting_too():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "request to %s failed", (SEND_URL,), None)
    try:
        raise RuntimeError(f"boom at {SEND_URL}")
    except RuntimeError:
        record.exc_info = sys.exc_info()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(logsafe.RedactFilter())
    handler.handle(record)
    assert "request to https://api.telegram.org/bot<приховано>/sendMessage failed" in stream.getvalue()
    assert "RuntimeError: boom at" in stream.getvalue() and TOKEN not in stream.getvalue()
    broken = logging.LogRecord("x", logging.INFO, __file__, 1, "no args for %s", (), None)
    assert logsafe.RedactFilter().filter(broken)  # криве форматування не валить фільтр


def test_install_filters_every_root_handler_once():
    root = logging.getLogger()
    handler = logging.StreamHandler(io.StringIO())
    root.addHandler(handler)
    try:
        logsafe.install()
        logsafe.install()
        assert [type(f) for f in handler.filters] == [logsafe.RedactFilter]
    finally:
        root.removeHandler(handler)
