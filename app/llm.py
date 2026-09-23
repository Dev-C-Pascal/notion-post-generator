"""ModelClient — інтерфейс до моделі. StubModelClient для тестів/прототипу;
RunPodModelClient — драфт пише модель ML-команди (RunPod Serverless), решта кроків поки stub.
"""
from __future__ import annotations

import os
import time
from typing import Protocol

import httpx

from .models import Article, Draft, Evaluation, Extraction, RelevanceResult

STUB_VERSION = "stub-v0"
RUNPOD_API = "https://api.runpod.ai/v2"


class ModelClient(Protocol):
    version: str

    def relevance(self, article: Article, topic: str | None) -> RelevanceResult: ...
    def extraction(self, article: Article) -> Extraction: ...
    def draft(self, article: Article, extraction: Extraction, n: int) -> Draft: ...
    def evaluate(self, draft: Draft) -> Evaluation: ...


class StubModelClient:
    """Заглушка: детермінована, без мережі."""

    version = STUB_VERSION

    def relevance(self, article: Article, topic: str | None) -> RelevanceResult:
        if topic and article.topic and (article.topic.lower() in topic.lower() or topic.lower() in article.topic.lower()):
            return RelevanceResult(article_id=article.id, relevant=True, score=0.9,
                                   reason=f"тема статті «{article.topic}» збігається із запитом «{topic}»")
        relevant = len(article.text) > 40
        return RelevanceResult(article_id=article.id, relevant=relevant, score=0.6 if relevant else 0.2,
                               reason="стаття достатньо змістовна (stub)" if relevant else "занадто коротка (stub)")

    def extraction(self, article: Article) -> Extraction:
        sentences = [s.strip() for s in article.text.replace("\n", " ").split(".") if s.strip()]
        return Extraction(
            article_id=article.id,
            main_claim=sentences[0] if sentences else article.title,
            facts=sentences[1:3],
            examples=[],
            angle=f"погляд Тимофія на тему «{article.topic or 'новини'}» (stub)",
        )

    def draft(self, article: Article, extraction: Extraction, n: int) -> Draft:
        # Поки нема моделі: «пост» = сама стаття без змін (заголовок + повний текст).
        return Draft(article_id=article.id, headline=article.title, text=article.text, model_version=self.version)

    def evaluate(self, draft: Draft) -> Evaluation:
        if len(draft.text) < 80:
            return Evaluation(article_id=draft.article_id, quality_score=0.3, failure_type="too_short")
        return Evaluation(article_id=draft.article_id, quality_score=0.7)


class ModelError(RuntimeError):
    """Модель не віддала пост (FAILED, таймаут, порожній output). Назва класу йде у failure_type прогону."""


def detect_lang(text: str) -> str:
    """uk, якщо кирилиці більше, ніж латиниці, інакше en. core.article.lang у базі Андрія поки порожній."""
    cyr = sum(1 for ch in text if "Ѐ" <= ch <= "ӿ")
    lat = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return "uk" if cyr > lat else "en"


class RunPodModelClient(StubModelClient):
    """Драфт — модель ML-команди на RunPod Serverless; relevance / extraction / evaluate — поки stub.

    POST /runsync {"input": {"article_text", "lang"}} → {"status": "COMPLETED", "output": {"post": "..."}}.
    На холодному старті runsync через ~90 с віддає IN_QUEUE / IN_PROGRESS без output — тоді опитуємо /status/{id}.
    """

    def __init__(self, endpoint_id: str, api_key: str, *, timeout_s: float = 600, poll_s: float = 5,
                 transport: httpx.BaseTransport | None = None):
        self.version = f"runpod-{endpoint_id}"
        self.base_url = f"{RUNPOD_API}/{endpoint_id}"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.timeout_s, self.poll_s, self.transport = timeout_s, poll_s, transport

    def draft(self, article: Article, extraction: Extraction, n: int) -> Draft:
        post = self._run({"article_text": article.text, "lang": detect_lang(article.text)})
        return Draft(article_id=article.id, headline=article.title, text=post, model_version=self.version)

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
    """Точка заміни: задані RUNPOD_ENDPOINT_ID і RUNPOD_API_KEY → драфт пише модель на RunPod, інакше stub."""
    endpoint_id, api_key = os.environ.get("RUNPOD_ENDPOINT_ID"), os.environ.get("RUNPOD_API_KEY")
    if endpoint_id and api_key:
        return RunPodModelClient(endpoint_id, api_key)
    return StubModelClient()
