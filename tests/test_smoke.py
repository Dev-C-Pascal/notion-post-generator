"""Smoke-тести без Notion: сервер стартує, бази створюються, вебхук приймається."""
import os

os.environ["NOTION_TOKEN"] = "secret_xxx"
os.environ["WEBHOOK_SECRET"] = "t"
os.environ["NOTION_DATABASE_ID"] = "0" * 32

from fastapi.testclient import TestClient

from app.main import app


def test_health_and_webhook():
    with TestClient(app) as c:
        assert c.get("/health").json()["ok"] is True
        assert len(c.get("/articles").json()) == 4
        assert c.post("/webhook", json={"data": {"object": "page", "id": "x"}}).status_code == 401
        r = c.post("/webhook", json={"data": {"object": "page", "id": "x"}}, headers={"x-webhook-secret": "t"})
        assert r.json() == {"accepted": True, "page_id": "x"}
