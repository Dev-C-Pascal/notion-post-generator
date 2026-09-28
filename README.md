# Comms Product — генератор драфтів постів (Notion → бекенд → модель → Notion)

Бекенд MVP Comms Product (KSE). Кнопка в Notion запускає прогін: бекенд бере свіжу статтю з відбору
Data Architect, модель ML-команди пише за нею пост у стилі Тимофія Милованова, драфт з'являється рядком
у таблиці MVP, де його оцінює рецензент.

Повна передача контексту (рішення, інциденти, відкриті питання) — у [`CONTEXT.md`](CONTEXT.md).

## Потік

```
Notion, сторінка «Сomms product» (воркспейс TM Space)
  кнопка FB ─► POST /webhook/fb   (український пост)
  кнопка X  ─► POST /webhook/x    (англійський тред)
        │  заголовок x-webhook-secret; бекенд одразу відповідає 202 {run_id, channel}
        ▼
FastAPI (EC2, Docker, Caddy TLS) → у фоні pipeline.run_pipeline:
  1. select  стаття з відбору Андрія (база comms, лише SELECT), з повним текстом і ще не взята:
             топ дня (ml.daily_pick) → якщо порожньо, тематична черга (marts.topic_queue).
             Стаття бронюється в postgen.used_articles ДО генерації — двічі не береться.
  2. draft   модель на RunPod Serverless, mode=draft_grounded: витягує факти, кожен перевіряє дослівною
             цитатою зі статті й пише пост лише з підтверджених; lang = uk (FB) | en (X), 3–5 хв.
             Відмова («немає підтверджених фактів») → стаття лишається використаною, береться наступна
             (не більше 2 разів); факти — у згорнутих блоках під постом і в postgen.drafts.extraction
  3. notion  рядок у таблиці MVP: Draft, Source, Status = «New draft»; тіло — run_id · модель · канал + пост
  4. store   postgen: drafts + draft_versions (v1); runs.db: журнал прогону
  збій → бронь знімається, стаття повертається в чергу
```

Черги статей наповнює стек Андрія на тому ж сервері (`/opt/comms`): `score_topics` двічі на годину,
`rank_daily` щогодини.

## Код

```
app/main.py      FastAPI: POST /webhook, /webhook/{fb|x}, /run; GET /health, /notion/check, /runs, /runs/{id}, /articles
app/pipeline.py  прогін: select → draft → notion → store; канал → мова
app/comms.py     читання статей з бази Андрія (топ дня, тематична черга)
app/llm.py       ModelClient + RunPodModelClient (runsync, опитування статусу, скасування через 10 хв,
                 mode, факти, GroundingRefused)
app/notion.py    Notion REST API 2022-06-28: створення рядка, тіло сторінки частинами по 100 блоків
app/pg.py        Postgres postgen: drafts, draft_versions, used_articles, sync_state
app/status.py    рядок статусу під кнопками в Notion: шукаю статтю → у черзі → генерується → готово / не вдалося
app/db.py        SQLite runs.db: журнал прогонів
app/models.py    pydantic-моделі кроків
tests/           unit, e2e з mock-Notion, API
```

## Змінні середовища

Шаблон — [`.env.example`](.env.example). Реальні значення лише в `.env` на сервері, не в git.

| Змінна | Що |
|---|---|
| `NOTION_TOKEN`, `NOTION_DATABASE_ID` | інтеграція Notion і таблиця MVP |
| `NOTION_PROP_DRAFT/SOURCE/STATUS` | назви колонок, якщо їх перейменують |
| `NOTION_STATUS_BLOCK_ID` | callout під кнопками: що з кожним натисканням (`app/status.py`); не задано — вимкнено |
| `WEBHOOK_SECRET` | має збігатися із заголовком `x-webhook-secret` у кнопках |
| `COMMS_DATABASE_URL` | база статей Андрія (без неї статей немає) |
| `POSTGEN_DB_PASSWORD` | наша база драфтів (сервіс `db` у compose) |
| `RUNPOD_ENDPOINT_ID`, `RUNPOD_API_KEY` | модель; без них `/health` = 500 і деплой відкочується |
| `RUNPOD_MODE` | `draft_grounded` (за замовчуванням) — пост лише з фактів, підтверджених цитатою; `draft` — старий режим |

## Локально

```bash
pip install -r requirements-dev.txt
ruff check app tests && mypy app && pytest -q
./run.sh    # http://localhost:8000
```

## Деплой

- `ci.yml` — на кожен push (крім комітів лише з `*.md`): ruff, mypy, pytest, `docker build` з тегом git sha.
- `deploy.yml` — **лише після зеленого CI** на push у `main` (або вручну): SSH на EC2 → checkout того sha,
  що пройшов CI → `IMAGE_TAG=<sha>` у `.env` → `docker compose up -d --build` → smoke `/health` → при збої відкат
  на попередній sha. Старіший коміт поверх новішого не деплоїться; один деплой за раз. Деплой перезапускає
  бекенд і обриває прогони, що йдуть: перед пушем перевірити `GET /runs`.
- `cron.yml` — `POST /run` лише вручну (Actions → Run workflow); щоденний розклад вимкнено 28.09.

Назовні (Caddy) відкриті лише `/webhook*`, `/run` і `/health`; решта — 404. Журнал прогонів і діагностика —
з сервера: `ssh … 'curl -s 127.0.0.1:8000/runs'`. Зміна `Caddyfile` деплоєм перестворює контейнер Caddy,
smoke перевіряє і бекенд, і HTTPS; синтаксис `Caddyfile` перевіряє CI.

Перезапуск на сервері вручну (тег образу поточної версії деплой пише в `.env`):

```bash
cd ~/post-generator && docker compose up -d app
```

Бекап бази драфтів — щоночі о 01:20 UTC, `ops/backup_postgen.sh` (crontab ubuntu), дампи в `~/backups`, 14 днів.
