# CONTEXT — Comms Product, бекенд-частина (Notion → webhook → pipeline → Notion)

Стан на 2026-09-17. Цей файл — повна передача контексту: що будуємо, що зроблено, як запустити,
що лишилось. Читати першим при поверненні до проєкту.

---

## 1. Що це за продукт

**Comms Product** (Products team KSE). Система, яка автономно проходить базу статей, обирає релевантні
для Тимофія Милованова, витягує головне з його перспективи і пише драфти постів у його стилі.
Запускається за розкладом або кнопкою в Notion. Під капотом — open-weight модель, дотренована на
постах Тимофія. Бекенд живе поза Notion; драфти повертаються в Notion (Content Pulse), де людина
approve / edit / reject.

**MVP — 2 тижні, демо до 25 вересня 2026.** Має показати: 20–50 тестових статей; relevance
(relevant / not з reason); extraction (main claim, facts, examples, angle); draft у Content Pulse;
evaluation (quality score + failure type).

**Out of scope MVP:** автопублікація, аналітика, стилі інших експертів, комерційний інтерфейс.

### Ролі
| Роль | Хто | Відповідає |
|---|---|---|
| Owner | Yevheniia Rui | vision, scope, success criteria |
| PM | Anton Sobkiv | hub, backlog, дедлайни |
| Data Architect | Andrii Kondratok | база статей і драфтів, схема, тестовий датасет |
| **Software Developer** | **Yevhen Liesnikov (я)** | **system design, бекенд, Notion-інтеграція, тести, CI/CD з rollback** |
| ML Engineers | Ihor Skrypko, Artem Khlusov, Yesieniia Smetanina | шаблони постів, стиль, relevance, extraction, evaluation |

Модель робить інший департамент (ML Engineers). Мій бекенд працює із заглушкою, інтерфейс зафіксований.

### Мої 4 задачі в Notion (Comms Product Hub, due 2026-09-17, High)
1. **Бази даних і доступ з коду** — схема статей (id, url, title, text, source, published_at,
   processed_flag), схема MVP Drafts (relevance, reason, score, extraction, failure_type, run_id,
   model_version, relation на статтю), NOTION_TOKEN + DATA_SOURCE_ID в env, модуль notion_client
   (fetch_articles, upsert_draft по article_id + run_id), fixture 20–50 статей.
2. **Backend: API і pipeline** — POST /run → 202 {run_id} async, GET /runs/{run_id}, GET /health;
   кроки як типізовані функції: fetch → relevance → extraction → draft → evaluate → write_notion;
   ModelClient як інтерфейс (stub / real), model_version на кожен драфт; run log (run_id, duration,
   articles_in, relevant_count, failure_type); тести unit/mock/e2e.
3. **Розгортання в хмарі** — Dockerfile + compose, env-конфіг, сервер з reverse proxy + TLS,
   секрети поза git, cron на POST /run, логи + /health + алерт.
4. **CI/CD з rollback** — CI на PR (ruff, mypy, pytest, build); CD на merge у main: образ з тегом
   git sha → deploy → smoke-тест; фейл → автовідкат; staging і prod; секрети окремо.

---

## 2. Архітектура (як працює під капотом)

```
Notion-кнопка «Trigger point» (page-level button, дія Send webhook)
        │  HTTP POST {"source":{...},"data":{<page object>}}  + header x-webhook-secret
        ▼
Публічний URL (зараз: cloudflared quick tunnel → localhost:8000; на проді: домен + Caddy TLS)
        ▼
FastAPI  POST /webhook  або  POST /run
        │  перевіряє x-webhook-secret, генерує run_id, відповідає 202 {"run_id":...} ОДРАЗУ
        │  (Notion чекає лише кілька секунд, тому робота йде у BackgroundTasks)
        ▼
pipeline.run_pipeline(run_id)
   1. fetch        — db.fetch_articles(limit=RUN_ARTICLES, processed_flag=0) з articles.db
                     (якщо база порожня → вбудована PLACEHOLDER-стаття, щоб кнопка все одно давала рядок)
   2. relevance    — ModelClient.relevance(article, topic) → RelevanceResult(relevant, reason, score)
   3. extraction   — ModelClient.extraction(article) → Extraction(main_claim, facts, examples, angle)
   4. draft        — ModelClient.draft(article, extraction, n) → Draft(headline, text, model_version)
   5. evaluate     — ModelClient.evaluate(draft) → Evaluation(quality_score, failure_type)
   6. write_notion — upsert у Notion-таблицю MVP: якщо (article_id, run_id) вже має notion_page_id
                     → PATCH /v1/pages/{id}; інакше POST /v1/pages з parent.database_id
                     Draft(title)=headline, Source=title — url, Status=Done|In progress, Score=quality
                     Тіло сторінки рядка = run_id · model · relevance · failure_type + повний текст
   → runs.db: runs (журнал прогону) + drafts (кожна стаття прогону, upsert по article_id+run_id)
   → articles.processed_flag = 1
```

**Чому Notion API-токен, а не MCP-конектор.** Вебхук іде лише в один бік (Notion → бекенд).
Щоб повернути результат, бекенд сам викликає Notion REST API з `Authorization: Bearer <token>`
internal integration. Інтеграція бачить лише сторінки, до яких її явно підключили через
«...» → Connections. MCP-конектор Claude — окремий OAuth-канал для асистента, бекенду не підходить.

**Чому тунель.** Сервер слухає localhost, Notion з інтернету його не бачить. cloudflared тримає
вихідне з'єднання до Cloudflare і проксіює вхідні запити назад. Адреса тимчасова, змінюється при
кожному перезапуску cloudflared → треба оновлювати URL у кнопці Notion.

---

## 3. Notion — що де

- **Воркспейс:** «Yevhen Liesnikov's Notion», ID `727916c1-4bf9-4868-9866-b65651e19866`,
  акаунт yliesnikov@kse.org.ua. Працюємо ТІЛЬКИ тут.
- **Сторінка «Сomms product»** `3de3973e-3325-8074-8011-c59043e0756e` — на ній кнопка «Trigger point»
  і вбудована таблиця MVP.
- **Таблиця MVP** database_id `3de3973e-3325-80bb-a91b-cd1f7f3244ae`,
  data_source `3de3973e-3325-8082-9ac4-000bc6f49192`.
  Властивості: Draft (title), Status (select: Not started / In progress / Done), Relevance (select,
  без опцій — бекенд НЕ чіпає, щоб не створювати опції), Score (number), Source (text), Link (url,
  з'явилась пізніше, бекенд поки не заповнює).
- **Кнопка:** When «Button is clicked» → Do «Send webhook». URL = `<tunnel>/webhook`,
  custom header `x-webhook-secret: change-me`, content «This page». Налаштовується тільки в UI Notion,
  API не дає доступу до дій кнопок.
- **Інтеграція** (internal integration, токен у `.env` як NOTION_TOKEN) підключена до сторінки
  «Сomms product» — перевірено через GET /notion/check, бекенд бачить таблицю MVP.
- Є також сторінка «1st edition» з діаграмою архітектури і таблицею «MVP Drafts» (Review queue,
  By status; колонки Draft, Status, Relevance, Score, Source, Link; рядок Test102). Якщо це ОКРЕМА
  таблиця від MVP — треба змінити NOTION_DATABASE_ID у `.env` на її id. Не перевірено.

### Інцидент, який треба закрити
На початку роботи Claude через Notion MCP-конектор (тоді авторизований як **Yevheniia Rui,
yrui@kse.org.ua**, воркспейс «Tymofiy Mylovanov's Space») створив без дозволу порожню приватну
базу **«Post Generator (prototype)»**: https://app.notion.com/p/10e0d745d6584d9186df85874c090acc
Через MCP її видалити неможливо (нема інструмента delete). **Видалити руками**: відкрити → «...» →
Delete. Робить Yevheniia, бо база в її Private. Нічого іншого в тому воркспейсі не змінювалось.
Після цього конектор переавторизовано на акаунт Yevhen Liesnikov.

### Безпека токенів
Обидва токени інтеграції були вставлені в чат (ntn_15422337829…UN6N6AR і ntn_15422337829…FOt8c8x).
**Перевипустити** в notion.so/profile/integrations після демо. У `.env` вписаний другий. Claude
токени у файли не вписує — це робить користувач сам.

---

## 4. Репозиторій і код

**GitHub:** https://github.com/Dev-C-Pascal/notion-post-generator (private, акаунт Dev-C-Pascal)
**Локально:** `~/Documents/Claude/post-generator` (окремий git-репозиторій, гілка main)

```
app/main.py       FastAPI: POST /run, POST /webhook, GET /health, GET /notion/check,
                  GET /articles, GET /runs, GET /runs/{run_id}
app/pipeline.py   run_pipeline(): 6 кроків, run log, upsert у Notion, PLACEHOLDER-стаття
app/llm.py        ModelClient (Protocol) + StubModelClient (stub-v0); get_model_client() — точка заміни
app/models.py     pydantic: Article, RelevanceResult, Extraction, Draft, Evaluation, DraftResult, RunSummary
app/notion.py     Notion REST (version 2022-06-28): get_page, get_database, create_row(+children body),
                  update_row, append_body, build_properties; текст ріжеться по 2000 символів (ліміт Notion)
app/db.py         SQLite: articles.db (articles) і runs.db (runs, drafts); DDL у константах
schema.sql        DDL обох баз (згенеровано з db.py)
tests/test_smoke.py  unit кроків моделі; e2e з mock-Notion (перевіряє upsert); API (401/202/404)
Dockerfile, .dockerignore, docker-compose.yml (app + caddy), Caddyfile
.github/workflows/ci.yml      ruff, mypy, pytest, docker build -t post-generator:<sha>
.github/workflows/deploy.yml  SSH на EC2: checkout <sha> → compose up → smoke /health → rollback на PREV
.github/workflows/cron.yml    щодня 07:00 UTC: POST $RUN_URL/run
pyproject.toml    ruff (line-length 130, E F I SIM), pytest (pythonpath ., asyncio auto), mypy
requirements.txt / requirements-dev.txt
run.sh            uvicorn app.main:app --reload --port 8000
.env.example      шаблон; .env у .gitignore
README.md         коротка інструкція
```

### .env (реальні значення НЕ в git)
```
NOTION_TOKEN=ntn_...                 # internal integration secret
WEBHOOK_SECRET=change-me             # має збігатися з header x-webhook-secret у кнопці
NOTION_DATABASE_ID=3de3973e-3325-80bb-a91b-cd1f7f3244ae
NOTION_PROP_DRAFT=Draft
NOTION_PROP_SOURCE=Source
NOTION_PROP_STATUS=Status
NOTION_PROP_SCORE=Score
RUN_ARTICLES=1                       # статей за один прогін (необов'язково, default 1)
DOMAIN=...                           # тільки для проду (Caddy)
```

### Схеми БД (SQLite зараз; рішення — Postgres на проді, та сама схема)
**articles.db / articles** (за Андрієм): id, url UNIQUE, title, text, source, published_at (ISO),
processed_flag 0/1, topic (необов'язкова підказка). Зараз ПОРОЖНЯ — наповнює Data Architect.

**runs.db / runs**: run_id PK, status (running|ok|failed), started_at, duration_ms, articles_in,
relevant_count, drafts_written, failure_type, model_version.

**runs.db / drafts**: id, run_id FK, article_id (логічний FK → articles.id), notion_page_id, relevance 0/1,
reason, score, extraction (JSON), headline, draft_text, failure_type, model_version, created_at,
UNIQUE(article_id, run_id) — ключ upsert.

**Аргументація Postgres:** три пов'язані сутності (статті, драфти, прогони) = реляційна модель;
датасет 10 000+ постів і база статей ростуть; processed_flag оновлюється конкурентно (cron + кнопка);
relevance потребує full-text search, згодом pgvector. Прототип на SQLite з ідентичною схемою,
перехід через рядок підключення.

---

## 5. Як запустити локально

```bash
cd ~/Documents/Claude/post-generator
pip install -r requirements-dev.txt
cp .env.example .env        # вписати NOTION_TOKEN (руками)
./run.sh                    # http://localhost:8000 ; бази data/*.db створюються самі
cloudflared tunnel --url http://localhost:8000   # видасть https://<random>.trycloudflare.com
```
Потім у Notion-кнопці URL = `https://<random>.trycloudflare.com/webhook`.

Перевірки:
```bash
curl localhost:8000/health
curl localhost:8000/notion/check                       # чи бачить інтеграція таблицю MVP
curl -X POST localhost:8000/run -H 'x-webhook-secret: change-me'   # → 202 {"run_id": "..."}
curl localhost:8000/runs/<run_id>
ruff check app tests && mypy app && pytest -q
```

### PyCharm
- File → Open → папка проєкту. Інтерпретатор: Settings → Project → Python Interpreter → Add Virtualenv,
  потім `pip install -r requirements-dev.txt`.
- Run config: Python → Module name `uvicorn`, Parameters `app.main:app --reload --port 8000`,
  Working directory = корінь проєкту.
- Database tool: «+» → Data Source → SQLite → File `data/articles.db` (і `data/runs.db`).
  Якщо джерело з червоним підкресленням: Properties → Download missing driver files → перевірити
  File-шлях → Test Connection → OK → розгорнути main → tables.
- Тести: правий клік на tests → Run pytest.

---

## 6. Статус по обіцяному (чесно)

| Задача | Зроблено | Не зроблено / умовно |
|---|---|---|
| 1. БД і доступ | схема articles за Андрієм; runs/drafts з run_id, model_version, failure_type, reason, score, extraction; env-конфіг; notion-клієнт; upsert по article_id+run_id; рядок створюється в MVP (перевірено на живій таблиці) | articles порожня (fixture 20–50 статей не робив — за домовленістю); relation на статтю в Notion-таблиці немає (є article_id у runs.db + Source url) |
| 2. Backend | POST /run 202 {run_id}; GET /runs/{id}; /health; 6 типізованих кроків pydantic; ModelClient stub; run log; 3 тести (unit, e2e mock, API) | e2e на 5 реальних статтях — нема статей; реальний ModelClient — чекаємо ML |
| 3. Хмара | Dockerfile, compose (app+Caddy TLS), env, секрети поза git, cron.yml, /health | НЕ викочено на сервер (вирішили не робити зараз); зараз працює локально через cloudflared; алертів немає; GPU/інференс — немає моделі |
| 4. CI/CD | CI зелений (ruff, mypy, pytest, build з тегом sha); deploy.yml зі smoke-тестом і rollback; environments staging/prod (staging default, prod через workflow_dispatch) | Deploy падає, бо нема secrets EC2_HOST/EC2_USER/EC2_SSH_KEY; rollback не перевірений на реальному сервері |

Формулювання для звіту: «Кнопка в Notion запускає прогін на бекенді, драфти приходять у таблицю MVP.
Обрали Postgres, прототип на SQLite з тією ж схемою. CI зелений, CD зі smoke-тестом і rollback
готовий, чекає на сервер.»

---

## 7. Наступні кроки

1. Видалити «Post Generator (prototype)» у воркспейсі Милованова (Yevheniia).
2. Перевипустити Notion-токени, оновити `.env`.
3. З'ясувати, чи «MVP Drafts» на сторінці «1st edition» — та сама таблиця, що MVP. Якщо ні — змінити
   NOTION_DATABASE_ID. Додати заповнення колонки Link (url статті) — 2 рядки в build_properties.
4. Андрій наповнює articles (SQL insert або CSV-імпорт; поле topic необов'язкове).
5. AWS: EC2 Ubuntu, порти 22/80/443, домен → IP; `git clone`, `.env` з DOMAIN; `docker compose up -d`;
   GitHub Secrets EC2_HOST, EC2_USER, EC2_SSH_KEY, RUN_URL, WEBHOOK_SECRET (окремо на staging і prod);
   у кнопці Notion замінити URL на `https://<домен>/webhook`.
6. Postgres: замінити sqlite3 у db.py на psycopg/SQLAlchemy з тим самим DDL (або RDS).
7. ML: реалізувати ModelClient з реальним інференсом, підключити в get_model_client() через env
   MODEL_BACKEND; писати Relevance select-опції, коли ML визначить словник.
8. Алерти: при status=failed у finish_run — повідомлення (Slack/Telegram webhook).

---

## 8. Правила роботи з Claude на цьому проєкті (вивчені на помилках)
- Нічого не створювати/змінювати/видаляти в Notion через MCP без прямого «так» на конкретну дію.
  Читати можна.
- Токени та секрети у файли вписує тільки користувач; Claude дає команду з плейсхолдером.
- Працюємо лише у воркспейсі Yevhen Liesnikov, поки не сказано інше.
