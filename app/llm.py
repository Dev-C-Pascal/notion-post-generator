"""ModelClient — інтерфейс до моделі. StubModelClient для тестів/прототипу;
реальний інференс (інший департамент) реалізує той самий інтерфейс.
"""
from __future__ import annotations

from typing import Protocol

from .models import Article, Draft, Evaluation, Extraction, RelevanceResult

STUB_VERSION = "stub-v0"


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


def get_model_client() -> ModelClient:
    """Точка заміни: тут інший департамент підставить реальний клієнт (за env MODEL_BACKEND)."""
    return StubModelClient()
