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


def write_headline(article: db.sqlite3.Row, topic: str | None, n: int) -> str:
    """Крок 2a: короткий заголовок (кілька слів) — іде в Draft. Заглушка: «Новина N: Україна <тема>»."""
    word = (topic or article["topic"] or "новини").capitalize()
    return f"Новина {n}: Україна {word}"


def write_post(article: db.sqlite3.Row, topic: str | None) -> str:
    """Крок 2b: LLM пише повний пост на основі статті — іде всередину сторінки рядка."""
    from datetime import datetime
    stamp = datetime.now().strftime("%H:%M:%S")
    return (
        f"[ЗАГЛУШКА {MODEL_NAME} · {stamp}] Пост на тему «{topic or 'без теми'}»\n\n"
        f"{article['body']}\n\n"
        f"Джерело: {article['title']} ({article['source']})\n"
        f"#KSE #AI"
    )


def score_article(article: db.sqlite3.Row, topic: str | None) -> float:
    """Крок 1b: оцінка релевантності статті (0..1). Заглушка: 0.9 якщо тема збіглася, інакше 0.5."""
    if topic and article["topic"] and article["topic"].lower() in topic.lower():
        return 0.9
    return 0.5
