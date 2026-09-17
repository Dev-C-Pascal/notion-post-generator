"""Заглушка LLM. Інтерфейс зафіксований — коли інший департамент дасть модель,
замінюється тільки тіло цих двох функцій (або клас-адаптер), решта системи не змінюється.
"""
import random

from . import db

MODEL_NAME = "stub-v0"


def select_article(topic: str | None) -> db.sqlite3.Row:
    """Крок 1: LLM обирає статтю, яка підходить під тему.
    Заглушка: якщо тема збігається з полем topic статті — беремо її, інакше випадкову.
    """
    articles = db.list_articles()
    if not articles:
        raise RuntimeError("База статей порожня")
    if topic:
        t = topic.lower()
        matches = [a for a in articles
                   if a["topic"] and (a["topic"].lower() in t or t in a["topic"].lower())]
        if matches:
            return matches[0]
    return random.choice(articles)


def write_post(article: db.sqlite3.Row, topic: str | None) -> str:
    """Крок 2: LLM пише пост на основі статті. Заглушка повертає шаблонний текст."""
    return (
        f"[ЗАГЛУШКА {MODEL_NAME}] Пост на тему «{topic or 'без теми'}»\n\n"
        f"{article['body']}\n\n"
        f"Джерело: {article['title']} ({article['source']})\n"
        f"#KSE #AI"
    )


def score_article(article: db.sqlite3.Row, topic: str | None) -> float:
    """Крок 1b: оцінка релевантності статті (0..1). Заглушка: 0.9 якщо тема збіглася, інакше 0.5."""
    if topic and article["topic"] and article["topic"].lower() in topic.lower():
        return 0.9
    return 0.5
