"""ModelClient — інтерфейс до моделі драфтів. RunPodModelClient — модель ML-команди на RunPod Serverless.

Заглушки на проді немає: без RUNPOD_ENDPOINT_ID і RUNPOD_API_KEY get_model_client() падає,
/health віддає 500, і деплой відкочується.
"""
from __future__ import annotations

import os
import time
from typing import Protocol

import httpx

from .models import Article, Draft

RUNPOD_API = "https://api.runpod.ai/v2"


class ModelClient(Protocol):
    version: str

    def draft(self, article: Article, lang: str | None = None) -> Draft: ...


class ModelError(RuntimeError):
    """Модель не віддала пост (FAILED, таймаут, порожній output). Назва класу йде у failure_type прогону."""


def detect_lang(text: str) -> str:
    """uk, якщо кирилиці більше, ніж латиниці, інакше en. core.article.lang у базі Андрія поки порожній."""
    cyr = sum(1 for ch in text if "Ѐ" <= ch <= "ӿ")
    lat = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return "uk" if cyr > lat else "en"


class RunPodModelClient:
    """POST /runsync {"input": {"article_text", "lang"}} → {"status": "COMPLETED", "output": {"post": "..."}}.

    lang — мова й формат поста, не мова статті: uk — довгий пост для FB, en — тред для X.
    Не задано — беремо мову статті. На холодному старті runsync через ~90 с віддає
    IN_QUEUE / IN_PROGRESS без output — тоді опитуємо /status/{id}.
    """

    def __init__(self, endpoint_id: str, api_key: str, *, timeout_s: float = 600, poll_s: float = 5,
                 transport: httpx.BaseTransport | None = None):
        self.version = f"runpod-{endpoint_id}"
        self.base_url = f"{RUNPOD_API}/{endpoint_id}"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.timeout_s, self.poll_s, self.transport = timeout_s, poll_s, transport

    def draft(self, article: Article, lang: str | None = None) -> Draft:
        lang = lang or detect_lang(article.text)
        post = self._run({"article_text": article.text, "lang": lang})
        return Draft(article_id=article.id, headline=article.title, text=post, model_version=self.version, lang=lang)

    def _run(self, payload: dict) -> str:
        deadline = time.monotonic() + self.timeout_s
        with httpx.Client(base_url=self.base_url, headers=self.headers, timeout=120, transport=self.transport) as c:
            job = c.post("/runsync", json={"input": payload}).raise_for_status().json()
            while job.get("status") in ("IN_QUEUE", "IN_PROGRESS"):
                if time.monotonic() > deadline:
                    c.post(f"/cancel/{job['id']}")  # щоб задача не палила GPU після нашого таймауту
                    raise ModelError(f"RunPod job {job['id']}: no result in {self.timeout_s:.0f}s")
                time.sleep(self.poll_s)
                job = c.get(f"/status/{job['id']}").raise_for_status().json()
        out = job.get("output")
        post = out.get("post") if job.get("status") == "COMPLETED" and isinstance(out, dict) else None
        if not isinstance(post, str) or not post.strip():
            raise ModelError(f"RunPod job {job.get('id')}: status={job.get('status')} error={job.get('error')!r}")
        return post.strip()


def get_model_client() -> ModelClient:
    endpoint_id, api_key = os.environ.get("RUNPOD_ENDPOINT_ID"), os.environ.get("RUNPOD_API_KEY")
    if not (endpoint_id and api_key):
        raise RuntimeError("RUNPOD_ENDPOINT_ID і RUNPOD_API_KEY не задані — драфт писати нічим")
    return RunPodModelClient(endpoint_id, api_key)
