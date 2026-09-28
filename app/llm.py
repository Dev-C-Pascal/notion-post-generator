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


class GroundingRefused(ModelError):
    """mode=draft_grounded: модель відмовилась писати — у статті немає фактів, які вона змогла підтвердити
    дослівною цитатою. Це властивість статті, а не збій: брати її вдруге марно."""


# відмову модель повертає по-різному: status FAILED + error «Extraction returned no parseable facts; refusing to
# draft.» (перевірено 29.09) або output.error «No extracted facts are grounded…; refusing to draft.» (за описом Артема)
REFUSAL_MARK = "refusing to draft"


def detect_lang(text: str) -> str:
    """uk, якщо кирилиці більше, ніж латиниці, інакше en. core.article.lang у базі Андрія поки порожній."""
    cyr = sum(1 for ch in text if "Ѐ" <= ch <= "ӿ")
    lat = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return "uk" if cyr > lat else "en"


class RunPodModelClient:
    """POST /runsync {"input": {"article_text", "lang", "mode"}} →
    {"status": "COMPLETED", "output": {"post": "...", "facts_used": [...], "facts_rejected": [...]}}.

    lang — мова й формат поста, не мова статті: uk — довгий пост для FB, en — тред для X.
    Не задано — беремо мову статті. mode — draft_grounded (з 29.09): модель витягує факти, кожен перевіряє
    дослівною цитатою зі статті і пише пост лише з підтверджених; draft — стара генерація по всій статті.
    На холодному старті runsync через ~90 с віддає IN_QUEUE / IN_PROGRESS без output — тоді опитуємо /status/{id}.
    """

    def __init__(self, endpoint_id: str, api_key: str, *, mode: str = "draft_grounded", timeout_s: float = 600,
                 poll_s: float = 5, transport: httpx.BaseTransport | None = None):
        self.mode = mode
        # режим — у версії: оцінки рецензентів до і після draft_grounded мають розділятись
        self.version = f"runpod-{endpoint_id}:{mode}"
        self.base_url = f"{RUNPOD_API}/{endpoint_id}"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.timeout_s, self.poll_s, self.transport = timeout_s, poll_s, transport

    def draft(self, article: Article, lang: str | None = None) -> Draft:
        lang = lang or detect_lang(article.text)
        out = self._run({"article_text": article.text, "lang": lang, "mode": self.mode})
        return Draft(article_id=article.id, headline=article.title, text=out["post"].strip(), model_version=self.version,
                     lang=lang, facts_used=_facts(out.get("facts_used")), facts_rejected=_facts(out.get("facts_rejected")))

    def _run(self, payload: dict) -> dict:
        deadline = time.monotonic() + self.timeout_s
        with httpx.Client(base_url=self.base_url, headers=self.headers, timeout=120, transport=self.transport) as c:
            job = c.post("/runsync", json={"input": payload}).raise_for_status().json()
            while job.get("status") in ("IN_QUEUE", "IN_PROGRESS"):
                if time.monotonic() > deadline:
                    c.post(f"/cancel/{job['id']}")  # щоб задача не палила GPU після нашого таймауту
                    raise ModelError(f"RunPod job {job['id']}: no result in {self.timeout_s:.0f}s")
                time.sleep(self.poll_s)
                job = c.get(f"/status/{job['id']}").raise_for_status().json()
        out = job.get("output") if isinstance(job.get("output"), dict) else {}
        error = out.get("error") or job.get("error")
        if error and REFUSAL_MARK in str(error):
            raise GroundingRefused(f"RunPod job {job.get('id')}: {error}")
        post = out.get("post") if job.get("status") == "COMPLETED" else None
        if error or not isinstance(post, str) or not post.strip():  # output.error без поста не постимо
            raise ModelError(f"RunPod job {job.get('id')}: status={job.get('status')} error={error!r}")
        return out


def _facts(value: object) -> list[dict]:
    """facts_used / facts_rejected: список {"claim", "verbatim_quote"}; рядок теж приймаємо як {"claim": ...}."""
    if not isinstance(value, list):
        return []
    return [f if isinstance(f, dict) else {"claim": str(f)} for f in value if f]


def get_model_client() -> ModelClient:
    endpoint_id, api_key = os.environ.get("RUNPOD_ENDPOINT_ID"), os.environ.get("RUNPOD_API_KEY")
    if not (endpoint_id and api_key):
        raise RuntimeError("RUNPOD_ENDPOINT_ID і RUNPOD_API_KEY не задані — драфт писати нічим")
    return RunPodModelClient(endpoint_id, api_key, mode=os.environ.get("RUNPOD_MODE", "draft_grounded"))
