# Post Generator — MLOps-прототип (Notion → webhook → backend → Notion)

## Потік
1. На сторінці Notion натискаєш кнопку → Notion шле POST `/webhook` (JSON з page object).
2. Бекенд (FastAPI) одразу відповідає `{"accepted": true}` і у фоні:
   - `articles.db` — обирає статтю (заглушка LLM: `app/llm.py`);
   - пише пост (заглушка LLM);
   - Notion API: створює рядок у таблиці MVP (`Draft`, `Source`, `Status=Done`, `Score`),
     або оновлює рядок, якщо кнопка стояла в рядку;
   - `posts.db` — журнал генерації (`posts.article_id → articles.id`).

## Структура
```
app/main.py    FastAPI: /webhook, /health, /notion/check, /articles, /posts
app/notion.py  клієнт Notion REST API (get page, create/update row)
app/db.py      дві SQLite-бази: articles.db, posts.db
app/llm.py     заглушка LLM: select_article, write_post, score_article
tests/         smoke-тести (без Notion)
```

## Локально
```bash
pip install -r requirements-dev.txt
cp .env.example .env            # вписати NOTION_TOKEN
./run.sh                        # http://localhost:8000
cloudflared tunnel --url http://localhost:8000   # публічний https для Notion
pytest -q
```

## Notion (один раз)
1. notion.so/profile/integrations → New integration → Internal Integration Secret → `.env` `NOTION_TOKEN`.
2. Сторінка з таблицею → `...` → Connections → додати інтеграцію.
3. Кнопка → Send webhook: URL `https://<host>/webhook`, header `x-webhook-secret: <WEBHOOK_SECRET>`, content `This page`.

## AWS (EC2, Docker)
1. EC2 Ubuntu, відкрити порти 22, 80, 443. DNS A-запис домену → IP інстансу.
2. На сервері: `sudo apt install -y docker.io docker-compose-v2 git`, `git clone <repo> ~/post-generator`.
3. `cp .env.example .env`, вписати `NOTION_TOKEN`, `WEBHOOK_SECRET`, `DOMAIN=<домен>`.
4. `docker compose up -d --build` → `https://<домен>/health`.
5. У Notion-кнопці замінити URL вебхука на `https://<домен>/webhook`.

## CI/CD (GitHub Actions)
- `ci.yml`: на кожен push — ruff, pytest, docker build.
- `deploy.yml`: push у `main` → SSH на EC2 → `git pull` → `docker compose up -d --build`.
  Secrets у репозиторії: `EC2_HOST`, `EC2_USER` (ubuntu), `EC2_SSH_KEY` (приватний ключ).

## Заміна заглушки на модель
Тільки `app/llm.py`: `select_article`, `write_post`, `score_article`. Решта не змінюється.
