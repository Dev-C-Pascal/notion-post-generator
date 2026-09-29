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


# Відмова (немає фактів / жоден не підтверджено / погані вхідні дані) з версії ендпоінта 15 (29.09) — завжди
# job COMPLETED + output.error_message. До v15 вона приходила як job FAILED + error «…refusing to draft.» (RunPod
# резервує ключ "error" у відповіді хендлера) — цей варіант теж розпізнаємо. "error" тепер лише справжній збій.
REFUSAL_MARK = "refusing to draft"


def detect_lang(text: str) -> str:
    """uk, якщо кирилиці більше, ніж латиниці, інакше en. core.article.lang у базі Андрія поки порожній."""
    cyr = sum(1 for ch in text if "Ѐ" <= ch <= "ӿ")
    lat = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return "uk" if cyr > lat else "en"


class RunPodModelClient:
    """POST /runsync {"input": {"article_text", "lang", "mode", "source_name"}} → {"status": "COMPLETED", "output": …}.

    lang — мова й формат поста, не мова статті: uk — довгий пост для FB, en — тред для X.
    Не задано — беремо мову статті. mode — draft_grounded (з 29.09): модель витягує факти, кожен перевіряє
    дослівною цитатою, пише пост і перевіряє його (детерміновано + суддя, одна повторна спроба);
    draft — стара генерація по всій статті без жодної перевірки. source_name — справжня назва видання з бази
    Андрія: лише її модель має право цитувати («— United24»); без неї пост не називає джерела взагалі.
    output.status: ok — post перевірено; needs_manual_review — post = null, є draft_for_review (НЕ перевірено)
    і verification з реченнями, що не пройшли; output.error_message без status — відмова ще до написання.
    На холодному старті runsync через ~90 с віддає IN_QUEUE / IN_PROGRESS без output — тоді опитуємо /status/{id}.
    """

    # 900 с: draft_grounded робить до 5 викликів моделі; 29.09 від запиту до відповіді минало до 572 с
    # (черга + холодний старт до 404 с) — 600 с різали б такі задачі.
    def __init__(self, endpoint_id: str, api_key: str, *, mode: str = "draft_grounded", timeout_s: float = 900,
                 poll_s: float = 5, transport: httpx.BaseTransport | None = None):
        self.mode = mode
        # режим — у версії: оцінки рецензентів до і після draft_grounded мають розділятись
        self.version = f"runpod-{endpoint_id}:{mode}"
        self.base_url = f"{RUNPOD_API}/{endpoint_id}"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.timeout_s, self.poll_s, self.transport = timeout_s, poll_s, transport

    def draft(self, article: Article, lang: str | None = None) -> Draft:
        lang = lang or detect_lang(article.text)
        payload = {"article_text": article.text, "lang": lang, "mode": self.mode}
        if article.source and article.source != "unknown":
            payload["source_name"] = article.source
        out = self._run(payload)
        needs_review = out.get("status") == "needs_manual_review"
        text = out["draft_for_review"] if needs_review else out["post"]
        meta = {k: out[k] for k in ("status", "regenerated", "trusted_source", "verification",
                                    "first_attempt_verification", "draft_reasoning") if out.get(k) is not None}
        return Draft(article_id=article.id, headline=article.title, text=text.strip(), model_version=self.version,
                     lang=lang, facts_used=_facts(out.get("facts_used")), facts_rejected=_facts(out.get("facts_rejected")),
                     needs_review=needs_review, review=meta)

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
        jid = job.get("id")
        out = job.get("output") if isinstance(job.get("output"), dict) else {}
        refusal = out.get("error_message")
        error = out.get("error") or job.get("error")
        if refusal or (error and REFUSAL_MARK in str(error)):
            raise GroundingRefused(f"RunPod job {jid}: {refusal or error}")
        if error or job.get("status") != "COMPLETED":
            raise ModelError(f"RunPod job {jid}: status={job.get('status')} error={error!r}")
        status = out.get("status")
        if status == "needs_manual_review":  # post навмисно null; draft_for_review — лише для людини
            text = out.get("draft_for_review")
        elif status in ("ok", None):  # None — старий mode=draft, без перевірки
            text = out.get("post")
        else:
            raise ModelError(f"RunPod job {jid}: невідомий output.status={status!r}")
        if not isinstance(text, str) or not text.strip():
            raise ModelError(f"RunPod job {jid}: status={status!r}, тексту немає")
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
