# CONTEXT — Comms Product, бекенд-частина (Notion → webhook → pipeline → Notion)

Стан на 2026-09-17. Цей файл — повна передача контексту: що будуємо, що зроблено, як запустити,
що лишилось. Читати першим при поверненні до проєкту.

> **Оновлення 2026-09-19:** бекенд розгорнуто на AWS (https://52-28-252-213.sslip.io), cloudflared
> більше не потрібен. Деталі, рішення і відкриті ризики — у **`AWS_CONTEXT.md`** (читати другим).
>
> **Оновлення 2026-09-20:** на тому ж сервері піднято базу Андрія (`comms-product-data`):
> Postgres 16 + pgvector, 6 731 стаття, 16 742 пости, 6 360 пар «стаття → пост», щоденний
> конвеєр з 8 задач. Статті більше НЕ порожні — але лежать не в нашій SQLite, а в окремій
> базі `comms`. Деталі — у **`DB_CONTEXT.md`** (читати третім).
>
> **Оновлення 2026-09-21:** бекенд підключено до бази Андрія. Прогін бере **найсвіжішу статтю**
> з `comms.core.article`, «пост» поки = стаття без змін, у Notion **Score більше не пишемо**,
> кожен драфт додатково лягає в **нашу окрему Postgres-базу `postgen`**. Задеплоєно й перевірено.
> Опис — у **§9** нижче. Схема системи: https://claude.ai/artifact/3dCaoyKQ4pYgw4t7FiukX8
>
> **Оновлення 2026-09-23/24:** заглушок більше немає. Пост пише **модель Артема на RunPod**,
> статтю обирає **векторний відбір Андрія** (топ дня, запасний — тематичний відбір), дві кнопки
> **FB (укр)** і **X (англ)**, у Notion — колонки рецензента і формула Score, одна стаття ніколи
> не береться двічі. Код Андрія на сервері оновлено до `b08aacf`. Опис — у **§10** нижче.
>
> **Оновлення 2026-09-27:** сторінку «Сomms product» з таблицею MVP і кнопками перенесено у воркспейс
> **TM Space**, бекенд пише туди. Код Андрія — `c5835a3` (міграція `0008`). Відбір статей
> (`score_topics`, `rank_daily`) **знову ввімкнено** за розкладом — черга статей більше не пустіє.
> Опис, стан систем і відкриті питання — у **§11** нижче (він важливіший за §3 і частину §10).
>
> **Оновлення 2026-09-28:** брейншторм ризиків (75 знахідок) і перша пачка виправлень: новий секрет вебхука,
> не більше 3 генерацій разом, повтори запису в Notion, лише свіжі статті, діагностика збоїв, нічний бекап
> `postgen`, деплой лише після зеленого CI. Інцидент з комітом, що видалив код (без наслідків для проду).
> Опис і що лишилось — у **§12** нижче.

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

> **Застаріло з 2026-09-27:** сторінка й таблиця тепер у воркспейсі TM Space, з новими ID. Актуальне — §11.
> Нижче — стан на 17.09, для історії.

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
4. ✅ ЗРОБЛЕНО 2026-09-20 (інакше, ніж планували) — Андрій віддав не CSV для нашої SQLite,
   а власний стек із Postgres. Розгорнуто на нашому ж сервері, див. `DB_CONTEXT.md`.
   Наша `articles.db` і далі порожня; статті тепер у `comms.core.article` (6 731 рядок).
5. ✅ ЗРОБЛЕНО 2026-09-19 (крім WEBHOOK_SECRET) — див. `AWS_CONTEXT.md`. AWS: EC2 Ubuntu, порти 22/80/443, домен → IP; `git clone`, `.env` з DOMAIN; `docker compose up -d`;
   GitHub Secrets EC2_HOST, EC2_USER, EC2_SSH_KEY, RUN_URL, WEBHOOK_SECRET (окремо на staging і prod);
   у кнопці Notion замінити URL на `https://<домен>/webhook`.
6. Postgres: замінити sqlite3 у db.py на psycopg/SQLAlchemy. **Змінилось 2026-09-20:** свій DDL
   уже не потрібен — на сервері є база `comms` зі схемою Андрія і реальними даними.
   Переписати `db.fetch_articles()` під `core.article`, узгодити з Андрієм, хто володіє
   драфтами (`ml.draft` у нього vs `runs.db/drafts` у нас). Див. `DB_CONTEXT.md` §9.
7. ✅ ЗРОБЛЕНО 2026-09-23 — драфт пише модель Артема на RunPod (`RUNPOD_*` в env, без MODEL_BACKEND),
   релевантність — векторний відбір Андрія. Див. §10.
8. Алерти: при status=failed у finish_run — повідомлення (Slack/Telegram webhook).

---

## 8. Правила роботи з Claude на цьому проєкті (вивчені на помилках)
- Нічого не створювати/змінювати/видаляти в Notion через MCP без прямого «так» на конкретну дію.
  Читати можна.
- Токени та секрети у файли вписує тільки користувач; Claude дає команду з плейсхолдером.
- Працюємо лише у воркспейсі Yevhen Liesnikov, поки не сказано інше. **З 2026-09-27 — у TM Space**
  (сторінка «Сomms product»), будь-яка зміна там — лише після «так» на конкретну дію.

---

## 9. Зміни 2026-09-21: статті з бази Андрія, окрема база драфтів, без Score

Коміти `452e5ca`, `3f911b6`, `5aaf28d` — задеплоєно (CI + Deploy зелені), перевірено живим прогоном
`39dffff0d2dd`: рядок у MVP з реальною статтею (РБК-Україна, 12 059 символів), Score порожній,
драфт і версія 1 у `postgen`.

### Як тепер іде прогін
```
кнопка / POST /run → 202 {run_id} → у фоні:
  fetch      comms.core.article: найсвіжіша за published_at, retrieval_status='full_text'
             (тільки SELECT; без COMMS_DATABASE_URL — SQLite + PLACEHOLDER, як раніше)
  relevance  stub (як було)
  extraction stub (як було)
  draft      stub: headline = заголовок статті, text = повний текст статті БЕЗ змін
  evaluate   stub, результат іде лише у Status (Done / In progress), у Notion цифр немає
  Notion     рядок у MVP: Draft(title), Source, Status. Score НЕ пишемо — оцінку ставитиме
             окрема система разом з людиною. Тіло: «run_id · model» + текст статті
             (>100 блоків дописуються пачками по 100 — ліміт Notion)
  postgen    drafts (upsert по run_id+article_id) + draft_versions v1
  runs.db    журнал прогону (як було)
```
Поки нових статей немає, кожне натискання дає новий рядок з тією самою статтею — так задумано.

### Нові файли й змінні
| Що | Де |
|---|---|
| `app/comms.py` | читання найсвіжішої статті з бази Андрія |
| `app/pg.py` | окрема база драфтів: `drafts`, `draft_versions`, `sync_state` |
| `COMMS_DATABASE_URL` | `postgresql://comms:<pw>@deploy-db-1:5432/comms` — пароль скопійовано з `/opt/comms/deploy/.env` |
| `POSTGEN_DB_PASSWORD` | пароль бази `postgen`, згенерований на сервері; `DRAFTS_DATABASE_URL` складає compose |
| `NOTION_PROP_SCORE` | більше не використовується (у серверному `.env` лишився, не заважає) |

Бекап `.env` до змін: `~/post-generator/.env.bak-20260921` (права 600).

### Інфраструктура (docker-compose.yml)
- Новий сервіс **`db`** — `postgres:16`, база/користувач `postgen`, том `pgdata`,
  `127.0.0.1:5433` (5432 зайнятий Андрієм), `shared_buffers=64MB`, healthcheck; `app` стартує після нього.
- `app` тепер у двох мережах: своїй і **`deploy_default`** (стек Андрія), щоб бачити `deploy-db-1`.
- Наша база має alias **`postgen-db`**: у мережі Андрія теж є сервіс `db`, і `db:5432` міг би
  потрапити в його базу. Не перейменовувати назад.

### Як подивитись руками
```bash
ssh -i ~/.ssh/post-generator-ec2 ubuntu@52.28.252.213
cd ~/post-generator && docker compose exec db psql -U postgen -d postgen
```
```sql
SELECT id, run_id, left(headline, 60), length(text), notion_page_id FROM drafts ORDER BY id DESC LIMIT 5;
SELECT notion_page_id, version, status, edited_by, captured_at FROM draft_versions ORDER BY id DESC LIMIT 10;
```

### Що не доробено / ризики
1. **Синк правок із Notion назад — лише заготовка.** Є таблиці `draft_versions` (v1 = що відправив
   бекенд) і `sync_state` (водяний знак), нормалізація тексту + хеш, `notion.query_edited_since()`,
   `notion.read_body()`, `pg.add_version()`. НЕ написано: цикл раз на 2 хв (опитування таблиці MVP
   за `last_edited_time`, тільки наші сторінки з `pg.known_pages()`, перший блок тіла — службовий
   рядок `run_id · model`, його відкидати), тести. Рішення: polling, а не Notion webhooks — без
   налаштувань в UI інтеграції і не губить пропущені події.
2. **У базу Андрія ходимо під його суперкористувачем `comms`.** Код робить лише SELECT, але
   правильніше окремий read-only користувач — узгодити з Андрієм.
3. **Залежність від імені мережі `deploy_default`.** Якщо Андрій перейменує папку `deploy` або
   compose-проєкт, наш `compose up` впаде → деплой відкотиться.
4. `postgen` не бекапиться.
5. ~~Status досі ставить stub-evaluate.~~ Закрито 2026-09-24: бекенд ставить лише «New draft», далі — людина (§10).

### Правило, додане 2026-09-21
- Робити рівно те, що просять: коли прохання тягне ширші зміни (інше джерело даних, чужа схема,
  FK) — спершу спитати. Коли Євген каже «як краще архітектурно» — рішення за Claude.
- 2026-09-21 Євген дозволив Claude самому дописати `POSTGEN_DB_PASSWORD` і `COMMS_DATABASE_URL`
  у серверний `.env` (значення генерувались/копіювались на сервері й не виводились). Це дозвіл на
  конкретну дію, а не зміна загального правила про секрети з §8.

---

## 10. Зміни 2026-09-23/24: модель на RunPod, відбір Андрія, дві кнопки, оцінювання в Notion

Коміти `7cfb202` → `c363a78` (усі задеплоєні, CI + Deploy зелені). Сервер зараз на `c363a78`.

| Коміт | Що |
|---|---|
| `7cfb202` | крок draft викликає модель Артема на RunPod |
| `54e266e` | стаття — з топу дня Андрія; прибрано всі заглушки |
| `4f900ae` | запасне джерело — тематичний відбір, поки топ дня рахується |
| `da8a871` | кнопка на канал: `/webhook/fb`, `/webhook/x` |
| `14b2203` | новий рядок у Notion отримує статус «New draft» |
| `53c46ea` | коментар: колонку Pass прибрано |
| `c363a78` | одна стаття ніколи не береться двічі (`used_articles`) |

### Як тепер іде прогін
```
кнопка FB / X / стара (або POST /run?channel=fb|x) → 202 {run_id, channel} → у фоні:
  select   відбір Андрія, лише статті з повним текстом, яких ще не брав жоден прогін:
             1) топ дня — ml.daily_pick, останній зріз, за rank;
             2) якщо там нічого — marts.topic_queue (above_threshold), за score, одна стаття — один рядок.
           Стаття БРОНЮЄТЬСЯ в postgen.used_articles ДО генерації (INSERT … ON CONFLICT DO NOTHING).
           Зайнята — береться наступна. reason = «топ дня №N, тема …, бал …» або «тематичний відбір …».
  draft    модель Артема на RunPod, lang за каналом: fb → uk, x → en, без каналу — мова статті.
  Notion   рядок у MVP: Draft = заголовок статті, Source, Status = «New draft».
           Тіло: «run_id · model · канал · мова» + текст поста.
  postgen  drafts + draft_versions v1 (як було).
  збій     якщо драфт не дійшов до Notion — бронь знімається, статтю можна взяти знову.
```
Кроків relevance / extraction / evaluate, тестової статті й SQLite-запасу більше немає.
Немає статей — прогін `ok` з 0 драфтів. Без `RUNPOD_*` в env `/health` віддає 500 → деплой відкотиться.

### Модель (Артем, RunPod Serverless)
- `POST https://api.runpod.ai/v2/pfgumkt2rbrwfi/runsync`, `Authorization: Bearer <RUNPOD_API_KEY>`,
  тіло `{"input": {"article_text": "...", "lang": "en"|"uk"}}` → `{"status": "COMPLETED", "output": {"post": "..."}}`.
- **`lang` — мова і формат поста, не мова статті**: `uk` → довгий український пост (FB), `en` → англійський
  тред «1/ 2/ …» (X). Перевірено: англійська стаття з `uk` дає український пост.
- Холодний старт: `runsync` через ~90 с віддає `IN_PROGRESS` без output → клієнт опитує `/status/{id}`;
  через 10 хв — `/cancel/{id}` і `ModelError`. Генерація 140–200 с, під навантаженням до ~13 хв.
- Env на сервері: `RUNPOD_ENDPOINT_ID=pfgumkt2rbrwfi`, `RUNPOD_API_KEY` (вписав Claude з дозволу Євгена
  2026-09-23; бекап `.env.bak-20260923`). `model_version` = `runpod-pfgumkt2rbrwfi`.
- Відомі вади моделі (передати Артему): тред закінчується на `NX` замість `N/`; додає факти, яких
  немає в статті (на тесті: «Financial Times», «12.4% in May», «flexible exchange rate by mid-2027»).
- **Не зроблено:** реєстрація в `ops.model_version` (реєстр Андрія, порожній). SQL підготовлено
  (name `runpod-pfgumkt2rbrwfi`, stage `draft`, adapter_ref `runpod:pfgumkt2rbrwfi`), не виконано —
  чекає рішення Євгена.

### Відбір Андрія на нашому сервері
- `/opt/comms` оновлено з `4a141d2` до **`b08aacf`** (bge-m3 + pgvector, міграції `0003`–`0007`,
  активна формула `transparent-v1`). Опис механіки — `/opt/comms/deploy/SELECTION.md`.
- **Дві локальні правки в `/opt/comms/deploy/Dockerfile`** (Андрій має закомітити обидві, інакше
  наступний `git pull` дасть конфлікт, а збірка з нуля впаде):
  1. `"lxml_html_clean"` поруч із trafilatura (стара правка з 20.09);
  2. `--extra-index-url https://pypi.org/simple` для torch — старий pip у Debian не ставить залежності
     лише з індексу PyTorch (`typing_extensions`, `flit_core`).
  Копія першої правки — `~/comms-local-fix-20260924.diff`.
- **Сервер НЕ збільшували** (рішення Євгена 2026-09-24): Андрій просить 8 ГБ, у нас 1,9 ГБ + 4 ГБ swap.
  Модель 2,3 ГБ живе здебільшого у swap. Перший ручний прогін: `score_topics` — 2 000 статей за 30 хв
  (~1,1/с, 529 над порогом), `rank_daily` — 62 хв (2 889 кандидатів + 2 966 історії), топ-30 готовий.
  Під час прогону `/health` відповідав 1–4 с, нічого не впало, CPU-кредити (unlimited) не просіли.
- **У `ops.job` вимкнено `score_topics`, `rank_daily`, `train_ranker`** (`enabled = false`).
  **Не вмикати без Євгена.** ← *Застаріло: 2026-09-27 Євген попросив, `score_topics` і `rank_daily`
  увімкнено, див. §11.* Наслідок: топ дня застиг на 24.09, `topic_queue` не поповнюється і
  звужується (вікно 3 дні). Якщо ввімкнути — модель постійно житиме в процесі планувальника (~2,5 ГБ).
  Запускати вручну: `cd /opt/comms/deploy && docker compose exec -T worker python -m pipeline run rank_daily`
  (дві задачі з моделлю одночасно не запускати — дві копії моделі не влізуть).

### Кнопки в Notion
- На сторінці «Сomms product» — кнопки **FB** (`https://52-28-252-213.sslip.io/webhook/fb`) і **X**
  (`…/webhook/x`), заголовок `x-webhook-secret`. Налаштовуються лише в UI Notion.
- Стара «Trigger point» (`/webhook`) працює як раніше — мовою статті. Невідомий канал → 404.
- Обидві кнопки беруть статті з однієї черги: статтю, взяту FB, кнопка X не отримає.

### Таблиця MVP (змінено через API з прямого «так» Євгена, варіант A)
| Колонка | Тип | Хто пише |
|---|---|---|
| Draft | title | бекенд |
| Source | text | бекенд |
| Status | select: New draft, Needs fact-check, Blocked — missing data, In review, Second opinion, Light edit, Heavy rewrite, Sent back to model, On hold, Approved as-is, Approved after edit, Synced to Content Pulse, Posted, Rejected — weak draft (+ старі Not started / In progress / Done) | бекенд ставить «New draft», далі людина |
| Article relevance | select yes / no (колишня Relevance) | рецензент |
| Fact safety | select ok / not ok | рецензент |
| Style score | select 1–5 | рецензент |
| Failure type | multi-select, 10 типів зі специфікації | рецензент |
| Score | formula | Notion |

- **Score:** 0, якщо Article relevance = no або Fact safety = not ok; порожній, поки не заповнені
  relevance, fact safety і style; інакше = Style score.
- **Source captured** свідомо не ведемо (домовленість з Євгеном). Колонку **Pass** (Score ≥ 4) додали
  і прибрали — плутала рецензентів.
- Status залишено типом select, бо тип Status (з групами To-do / In progress / Complete) через API
  не налаштовується. Перейти на нього — Євген у UI + бекенд писати `{"status": …}`.
- **Можна міняти без коду:** додавати/ховати колонки, view, правити текст і статуси.
  **Не можна:** перейменовувати Draft / Source / Status (тоді — `NOTION_PROP_*` у серверному `.env`),
  міняти їхній тип, видаляти опцію «New draft», видаляти/переносити таблицю без перемикання токена.
- Оцінки живуть лише в Notion; синк у базу (§9, ризик 1) досі не написано.

### `used_articles` (postgen) — щоб стаття не бралась двічі
- Причина: стаття позначалась використаною лише після запису драфту (~5 хв), тож кілька натискань
  поспіль брали ту саму: 4 драфти «Russia's Jet Drone Use…» за 5 с (24.09), 8 × Harvey Weinstein (23.09).
- `article_id` PK, `article_url` UNIQUE, `run_id`, `channel`, `claimed_at`. На старті бекенду туди
  доливаються всі статті з `drafts` (на 24.09 — 10).
- Бронь знімається, якщо драфт не дійшов до Notion. Прогін, обірваний деплоєм, лишається `running`
  у `runs.db`, і його бронь не знімається — стаття випадає з черги.

### Що не доробено / ризики (станом на 24.09)
1. **Паралельні натискання перевантажують RunPod.** Тестувальники тиснуть по 5 разів поспіль:
   із 10 прогонів 16:59–17:00 (Київ) у Notion дійшов 1 — 5 не дочекались моделі за 10 хв,
   4 згенерували текст і впали на `ConnectTimeout` до Notion. Запропоновано (не зроблено):
   обмеження одночасних генерацій з чергою на нашому боці + повтор запису в Notion.
   Поки — просити тиснути один раз і чекати 3–7 хв.
2. ✅ ЗРОБЛЕНО 2026-09-27, див. §11. **Перенесення таблиці в інший Notion-спейс** (платний план) — чекаємо від адміна токен нової
   інтеграції. Порядок: Євген переносить сторінку «Move to» → одразу новий `NOTION_TOKEN` у серверний
   `.env` → Claude знаходить новий `NOTION_DATABASE_ID`, `docker compose up -d app`, `/notion/check`,
   живий прогін. Між переносом і перемиканням кнопки падатимуть.
3. **`Scheduled run` (cron.yml) падає щодня з 22.09** — причину знайдено 27.09, див. §11. Коли запрацює, б'є в `/run`
   без каналу (мовою статті).
4. `drafts` у postgen не зберігає канал / мову (канал є лише в `used_articles` і в тілі Notion).
5. **Ключ RunPod був у чаті відкритим текстом** — перевипустити після демо разом із Notion-токенами.

### Правила, додані 2026-09-23/24
- Зміни структури таблиці в Notion — тільки після прямого «так» на конкретний список змін
  (так і зроблено 24.09: колонки, статуси, формула; окремо — видалення Pass).
- Пушити в `main` = деплоїти = перезапуск бекенду: прогони, що йдуть у цю мить, обриваються.
  Перед пушем перевіряти `GET /runs` і чекати, поки `running` не буде.
- 2026-09-23 Євген дозволив Claude самому вписати `RUNPOD_*` у серверний `.env` — дозвіл на
  конкретну дію, як і 21.09.

---

## 11. Зміни 2026-09-27: Notion у TM Space, відбір увімкнено, код Андрія `c5835a3`

Коміт `f536d86` (новий `NOTION_DATABASE_ID` у `.env.example`) — задеплоєно, CI + Deploy зелені.

### Notion переїхав у TM Space
| Що | Значення |
|---|---|
| Воркспейс | **TM Space** (Tymofiy Mylovanov's Space, платний план) |
| Сторінка «Сomms product» | `0c0d1fbd-7688-8333-976a-015684f34429` |
| Таблиця MVP | `144d1fbd-7688-835c-b479-81680a5dbc0f` — 77 рядків перенеслись, усі колонки й формула Score на місці |
| Інтеграція | нова, від адміна TM Space; токен у серверному `.env` (вписав Claude з дозволу Євгена) |

- **«Move to» між воркспейсами = копія.** Усі ID нові, оригінал лишається у старому воркспейсі
  (`3de3973e-…`). У колонці **Created time** у всіх 77 перенесених рядків — 27.09 09:49 (момент копії).
  Справжній час — у `postgen.drafts.created_at`. Колонку з правильним часом Євген вирішив не додавати (MVP).
- **Сервер:** `.env` — новий `NOTION_TOKEN` і `NOTION_DATABASE_ID` (бекап `.env.bak-20260927`).
  Тестовий рядок створено й одразу архівовано.
- **`notion_page_id` перепрописано** на нові сторінки для 77 драфтів у `postgen` (`drafts`, `draft_versions`)
  і в `runs.db`; зіставлення — за `run_id:` з першого рядка тіла сторінки. 16 (`postgen`) і 20 (`runs.db`)
  старіших драфтів, чиї сторінки видалили ще до переносу, лишились зі старими ID.
  Бекапи: `~/postgen-drafts-bak-20260927.sql`, `~/runs.db.bak-20260927`.
- Кнопки на новій сторінці перевірено: вебхуки з `0c0d1fbd-…` доходять, прогони йдуть.

### Інцидент: старий образ 42 секунди
Після заміни `.env` виконано голий `docker compose up -d app`. Образ у compose — `post-generator:${IMAGE_TAG:-local}`,
в `.env` `IMAGE_TAG` немає, тож піднявся **`post-generator:local` від 19.09 (заглушка моделі)**. Працював
15:29:51–15:30:33 (Київ), прогонів за цей час не було. Помічено за `/health` → `stub-v0`.
**Правило:** вручну перезапускати лише `IMAGE_TAG=$(git rev-parse HEAD) docker compose up -d app`
і перевіряти `docker inspect post-generator-app-1 --format '{{.Config.Image}}'`.

### Стек Андрія: `b08aacf` → `c5835a3`
- Два коміти від 24.09, міграція **`0008_coverage`** (застосувалась сама при старті воркера):
  перед ранжуванням відкидаються статті, за посиланням на які вже є наш пост або які вже в дайджесті;
  вікно свіжості 24 год (було 36). Перевірка «за схожістю» вимкнена (`covered_vector=false`).
- Таблиці, з яких читає наш бекенд (`ml.daily_pick`, `marts.topic_queue`, `core.article`,
  `ops.candidate_pool`), міграція не змінює. `marts.daily_top` перестворено — ми його не читаємо.
- Локальні правки Андрія в `deploy/Dockerfile` (`lxml_html_clean`, `--extra-index-url`) і
  `deploy/docker-compose.override.yml` лишились, конфлікту не було. **Андрію досі треба закомітити
  правки Dockerfile і знати, що його стек оновлено.**

### Відбір статей увімкнено
- **Причина:** черга спорожніла. Топ дня застиг на 24.09, усі його статті вже взято; `topic_queue`
  (вікно 3 дні) — 0. Кожне натискання давало «ok, 0 драфтів» за 0,1 с, бекенд не звертався до RunPod —
  звідси «воркери RunPod не запускаються». Сам RunPod справний (перевірено тестовою задачею:
  старт воркера 69 с, генерація 101 с).
- Ручний `rank_daily` — 23,5 хв (1 504 кандидати → топ-30). Потім у `ops.job` **увімкнено
  `score_topics` (:15, :45) і `rank_daily` (:55)**; `train_ranker` лишився вимкненим.
- Модель bge-m3 (~2,3 ГБ) живе в процесі планувальника, здебільшого у **swap 4 ГБ** (`/swapfile`,
  з 20.09). Перші два `score_topics` розгрібали накопичене (2 × ~27 хв, по 2 000 статей) — планувальник
  у цей час стояв: збір новин чекав, `rank_daily` о 17:55 пропустився. Далі кожна задача ~1 хв.
- **Не запускати `score_topics`/`rank_daily` вручну**, поки розклад увімкнено: друга копія моделі
  не влізе (блокування Андрія не дає перетнутись лише однаковим задачам).
- Вимкнути: `UPDATE ops.job SET enabled=false WHERE job IN ('score_topics','rank_daily');`

### RunPod
- Ендпоінт `pfgumkt2rbrwfi` (mamay-tymofiy-serverless), образ `…-main-dockerfile:c573e2e1f`. Чи це
  останній коміт — невідомо: репозиторій ML-команди нашому GitHub-акаунту недоступний (404).
- До 10 воркерів, GPU 16–24 ГБ (A4000/A5000/3090/L4…), `$0.59/год` на воркер, ~1,5 ¢ за драфт.
- **Баланс $7.87** (27.09 вечір) — вистачить на ~500 драфтів. Поповнює власник акаунта (ML-команда).
- **`executionTimeout` ендпоінта — 5 хв**: під навантаженням генерація тривала до 13 хв — такі задачі
  RunPod обірве. Передати Артему. Модель і далі додає факти, яких немає в статті.

### `Scheduled run` (cron.yml) — причина
Падає з **401**: секрет `WEBHOOK_SECRET` у GitHub порожній. Не лагодили. Команда, що генерує секрет і
вписує його і в `.env`, і в GitHub, — `AWS_CONTEXT.md` (розділ про WEBHOOK_SECRET). Після виправлення
щодня буде прогін без каналу (мовою статті) — вирішити, чи він потрібен.

### AWS — витрати (оцінка)
IAM-користувач `Claude-cliu` не має прав на білінг/Cost Explorer/Pricing — точних сум немає. Ресурси:
один `t3.small` (unlimited, доплат за CPU 0 — середнє 0,6–7 %), диск 40 ГБ gp3, Elastic IP, трафік 0,5 ГБ.
**≈ $25/міс** (≈ $7.4 з 19.09). Акаунт на Free plan — імовірно, списується з кредитів. Точні цифри —
консоль Billing → Bills / Credits; для Claude — додати `Claude-cliu` політику `AWSBillingReadOnlyAccess`.

### Стан на вечір 27.09
| Що | |
|---|---|
| Драфтів у таблиці MVP | 101 (77 перенесених + 24 за 27.09), усі «New draft» |
| Драфтів у `postgen` | 117 |
| Статей доступно кнопкам | 427 (топ дня 4 + тематична черга 423), ще 227 чекають повного тексту |
| Прогони кнопок з 17:00 | 24: 23 драфти, 1 збій (`ConnectTimeout`, 8 натискань одночасно) |
| Сервер | ~0,9 ГБ RAM вільно, swap 1,6 ГБ, диск 47 %, `/health` 0,05–1 с |

### Відкриті питання (27.09)
1. **Старий воркспейс:** прибрати кнопки FB/X/Trigger point зі старої сторінки `3de3973e-…` — нею
   користувались і після переносу. Потім видалити сторінку. *(28.09: кнопки вже не працюють — у них старий
   секрет; саму сторінку ще видалити.)*
2. **Інтеграція TM Space бачить ~100 баз** — попросити адміна обмежити її сторінкою «Сomms product».
3. **Перевипустити** новий Notion-токен (був у чаті), старі Notion-токени, ключ RunPod.
4. Баланс і таймаут RunPod — до Артема.
5. Андрію — закомітити правки Dockerfile; повідомити про оновлення стеку і ввімкнений відбір.
6. ✅ 28.09 — розклад `Scheduled run` вимкнено (§12).
7. ✅ 28.09 — не більше 3 генерацій разом, повтори запису в Notion (§12).
8. Бекап `postgen` — ✅ 28.09 (§12). Budget-алерту в AWS досі немає.

---

## 12. Зміни 2026-09-28: брейншторм ризиків і перша пачка виправлень

### Брейншторм
Мультиагентний аналіз (4 лінзи: код, бізнес-логіка, інтеграції/інфра/безпека, продукт; кожну знахідку
перевіряв окремий скептик): **75 знахідок, спростовано 0**. Найважливіше з цифрами:
- секрет вебхука на сервері був `change-me`, `/openapi.json` і `/docs` відкриті — будь-хто міг запускати генерацію;
- усі `ConnectTimeout` до Notion — через спільний пул потоків (6 на 2 CPU), зайнятий генераціями;
- готовий пост губиться при збої запису в Notion (зберігається лише після Notion, повторів не було);
- запасна черга давала статті з медіанним віком 59 год; 20 з 21 оціненого рядка — звідти;
- модель не знає джерела статті (отримує лише текст): Fact safety «not ok» у 16 з 19 оцінених;
- 73 з 79 X-тредів закінчуються на «NX» замість «N/»; ~20 тредів мають твіт > 280 символів;
- оцінки рецензентів (19 рядків, 18 коментарів) — лише в Notion, синку немає.

### Зроблено (усе задеплоєно, CI + Deploy зелені)
| Коміт | Що |
|---|---|
| `f34e722` | `cron.yml`: розклад вимкнено, лишився ручний запуск |
| `9f1a9c3` | бекенд не стартує з порожнім / `change-me` секретом; `hmac.compare_digest`; не-JSON тіло → не 500 |
| `3f57371` | модель — у власному пулі на 3 потоки (`MAX_PARALLEL_DRAFTS`): не більше 3 генерацій разом, Notion не чекає |
| `6fed5bb` | Notion: до 4 спроб на ConnectError/ConnectTimeout/429 (`Retry-After`); ReadTimeout і 5xx не повторюються (дубль) |
| `2ad60e2` | відбір: зріз топу дня старший за 3 год — пропускається; запасна черга — лише статті за 24 год; чесний reason |
| `6ca5c7f` | `runs.db`: `failure_step` (select/model/notion/store) і `failure_detail` (стаття + текст помилки); `finish_run` у try; логи у `data/app.log` (5 МБ × 3) |
| `fc794a8` | `deploy.yml`: `IMAGE_TAG` пишеться в `.env`; `ops/backup_postgen.sh` |
| (цей) | деплой **лише після зеленого CI** (`workflow_run`), один за раз, старіший коміт поверх новішого не деплоїться; `*.md` не запускають ні CI, ні деплой |

**На сервері (вручну):**
- **`WEBHOOK_SECRET` замінено** (бекап `.env.bak-20260928`). Новий секрет Євген вписав у кнопки **FB** і
  **Twitter** у TM Space; кнопки старої сторінки після цього отримують 401. Перевірено живим прогоном.
- **Нічний бекап `postgen`**: crontab ubuntu, 01:20 UTC → `~/backups/postgen-YYYY-MM-DD.dump` (`pg_dump -Fc`),
  14 днів, лог `~/backups/postgen-backup.log`. Перший дамп 200 КБ перевірено (`pg_restore --list`). Той самий диск —
  від втрати інстансу не рятує (потрібні EBS-снапшоти, їх налаштовує власник акаунта).
- **Termination protection** інстансу ввімкнено.
- Образ `post-generator:local` видалено; `IMAGE_TAG` у `.env` → голий `docker compose up -d app` безпечний.

### Інцидент: коміт, що видалив код
Між комітами `6ca5c7f` і `59b8d2b` локальний `.git/index` спорожнів (причину не знайдено — жодна команда
Claude індекс не чіпала; можливо, інша програма паралельно працювала з git). `git add <3 файли> && git commit`
дав коміт лише з цими трьома файлами — **24 файли видалено** (app/, docker-compose.yml, Dockerfile, ci.yml…).
Його запушено. CI не запустився (ci.yml теж видалено), deploy витягнув коміт на сервер і впав на
`no configuration file provided` **до** перезапуску контейнерів → прод весь час працював на `335c3fa`,
наслідків немає. Виправлено комітом `fc794a8` (`git read-tree 6ca5c7f` + ті самі 3 файли).
**Уроки:** перед кожним комітом дивитись `git diff --cached --stat HEAD`; деплой тепер чекає на CI —
без ci.yml або з червоним CI він не запуститься.

### Стан на 28.09
- Статей доступно кнопкам: 89 (топ дня 11 + запасна черга за 24 год 78); `rank_daily` і `score_topics` — за розкладом.
- Сервер: `fc794a8`, `/health` 200; `runs.db` має нові колонки; логи — `~/post-generator/data/app.log`.

### Що лишилось (з брейншторму, за пріоритетом)
1. ✅ 28.09 — **публічні маршрути закрито в Caddy**: назовні лише `/health`, `/webhook`, `/webhook/*`, `/run`,
   решта (`/runs`, `/runs/{id}`, `/notion/check`, `/articles`, `/docs`, `/openapi.json`) — 404. Дивитись
   `/runs` — лише з сервера (`curl 127.0.0.1:8000/runs`). Деплой перестворює Caddy, якщо змінився `Caddyfile`,
   і smoke-тестує HTTPS через `--resolve`; CI валідує `Caddyfile` (`caddy validate`).
2. **Видимий слід у Notion** при натисканні (рядок «Генерується…» / «Failed» / «Немає статей» або статус-блок) —
   інакше мультиклік; нові опції Status — лише після «так».
3. **Зберігати текст до запису в Notion** (`pending_notion` у postgen, flush на старті) — друга лінія захисту.
4. **Контекст для рецензента** в рядку: reason, видання, дата, посилання, toggle з текстом статті.
5. **Постобробка X**: `NX` → `N/`, прапорець для твітів > 280, сирий текст — у postgen.
6. **Поля в `drafts`**: канал, мова, джерело відбору, rank, зріз, candidate_id — для evaluation.
7. **`/ready`** за секретом: postgen, контракт comms (наші 5 відношень), Notion, скільки статей доступно.
8. **Денний ліміт генерацій**; баланс RunPod (~$7.9) і таймаут 5 хв — до Артема.
9. **Метадані джерела на вході моделі** (експеримент / контракт з Артемом) — головна причина галюцинацій.
10. Синк оцінок рецензентів у postgen; дедуп за подією; reconciliation `running`-прогонів на старті.
11. **Безпека**: перевипустити AWS-ключ `Claude-cliu` (EC2FullAccess) і видалити CSV з ключами з `~/Downloads`;
    прибрати `.env.bak-*` після ротації токенів; read-only роль у базі Андрія (з його OK).
