# Family EA — MVP Spec v3

Від v2 додано: web view, voice input (з першого зрізу), `[http_service]` на Fly. Ядро без змін: три таблиці, один LLM call, один ранковий пуш, два зрізи.

---

## 1. Ідея і гіпотеза

Приватний сімейний помічник у Telegram для двох людей. Кожен пише або наговорює боту природною мовою, не вирішуючи, чи це task, note чи contact. Бот тримає спільний контекст, зранку показує день, відповідає на питання. Web view показує, що система насправді зберегла.

**Гіпотеза:**

> Якщо двоє людей просто кидають EA шматки контексту протягом дня, чи зможе він тримати open loops і повертати потрібне у правильний момент?

Все, що не перевіряє цю гіпотезу, — не в MVP.

---

## 2. UX

### Telegram

Два приватних чати з одним ботом, спільний family state. Все, що написав один, бачить другий.

Anna:
> Завтра після школи попрати форму Олі.

Bot:
> Записав на завтра.

Oleh (voice):
> Приходив газовик Петро, замінив клапан у котлі, номер плюс три вісім ноль…

Bot:
> Записав.

Зранку о 8:00 бот сам пише кожному:

> Сьогодні: попрати форму Олі. Стоматолог о 15:30.
> Ще висить: поговорити з пані Марією (до 20.09), продукти мамі.
> Нічого не забув?

Anna:
> Марію закрий, ми вчора говорили. І додай — купити Олі зошити.

Bot:
> Закрив Марію. Додав зошити.

### Web

Одна сторінка за basic auth. Відкриті commitments зверху (сьогодні / прострочено / решта) з кнопкою done, memories нижче від нових до старих, пошук. Це debug-view і водночас зародок інтерфейсу.

---

## 3. Головний принцип

**LLM розуміє, код виконує.** LLM один раз на повідомлення повертає structured output: що запам'ятати, які петлі відкрити, змінити, закрити. Все інше — ранковий пуш, вибірка "що сьогодні", web, зберігання — детермінований код. LLM нічого не пам'ятає між викликами.

---

## 4. Дані

SQLite, три таблиці. Оригінальні повідомлення ніколи не змінюються.

```sql
CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  user_id TEXT NOT NULL,            -- 'oleh' | 'anna' | 'bot'
  tg_message_id INTEGER,
  created_at TEXT NOT NULL,         -- ISO UTC
  raw_text TEXT NOT NULL,           -- для voice: транскрипт
  is_voice INTEGER DEFAULT 0,
  llm_result TEXT                   -- JSON того, що LLM повернув; для /debug і web
);

CREATE TABLE memories (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  deleted_at TEXT                   -- soft delete
);

CREATE TABLE commitments (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  owner TEXT,                       -- хто робить; NULL = обидва / неважливо
  status TEXT NOT NULL,             -- 'open' | 'done' | 'dropped'
  due_at TEXT,                      -- ISO datetime, якщо є конкретний час
  due_from TEXT,                    -- ISO date, м'яке вікно
  due_to TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  closed_at TEXT
);

CREATE VIRTUAL TABLE memories_fts USING fts5(text, content='memories', content_rowid='id');
```

`created_by` зберігається всюди, хоч зараз не використовується — щоб privacy можна було додати без міграції.

### people — `family.yaml`

Не в базі. Читається при старті, йде в prompt. `telegram_id` — це також allowlist.

```yaml
oleh:
  name: Олег
  telegram_id: 123456
  role: family
anna:
  name: Анна
  telegram_id: 234567
  role: family
olia:
  name: Оля
  role: child
mariia:
  name: пані Марія
  role: teacher
  related_to: olia
```

---

## 5. Пайплайн одного повідомлення

```text
1. якщо voice → скачати ogg → транскрипція → текст
2. зберегти message
3. зібрати контекст
4. один structured-output call
5. застосувати операції
6. відповісти
```

### 5.2 Контекст для LLM

- `family.yaml`
- поточна дата/час, `Europe/Kyiv`
- всі відкриті commitments (з id)
- блок `today / overdue` (той самий, що для ранкового пушу, §6)
- memories за останні 60 днів (з id)
- FTS5 top-10 memories за словами з повідомлення — для старіших
- останні 20 messages обох юзерів і бота

Це влазить у контекст із запасом навіть на кількох сотнях записів. Embeddings — коли перестане влазити або коли ціна за повідомлення почне муляти.

### 5.3 Output schema

```json
{
  "reply": "текст відповіді",
  "memories": [
    { "op": "create", "text": "Газовик Петро замінив клапан у котлі 9.09. Тел +380…" },
    { "op": "delete", "id": 5 }
  ],
  "commitments": [
    { "op": "create", "text": "Попрати форму Олі", "owner": "anna", "due_from": "2026-09-10" },
    { "op": "create", "text": "Стоматолог", "owner": "anna", "due_at": "2026-09-10T12:30:00Z" },
    { "op": "update", "id": 12, "due_to": "2026-09-23" },
    { "op": "close", "id": 7, "status": "done" }
  ]
}
```

Порожні масиви — нормальна відповідь на питання або "дякую". Великий копі-паст із двадцятьма нотатками → двадцять `create` в одному output.

### 5.4 Виправлення

Без reply-to. "Ні, з Марією не до п'ятниці, а протягом двох тижнів" — LLM бачить відкриті commitments і останні повідомлення, повертає `update`. "Забудь про газовика" → `delete`. Якщо неоднозначно — LLM перепитує в `reply` і повертає порожні ops.

### 5.5 Код після call

- apply ops у порядку: memories → commitments
- `close` на закритий або неіснуючий id — ігнорувати, залогувати
- відповісти `reply`
- зберегти відповідь бота в `messages` з `user_id='bot'`

### 5.6 Voice

Telegram voice message → `getFile` → ogg → transcription API (OpenAI Whisper або Deepgram, мова `uk`) → текст у той самий пайплайн. У `reply` бот першим рядком показує транскрипт, щоб було видно, що він почув:

> 🎙 "Приходив газовик Петро, замінив клапан…"
> Записав.

---

## 6. Ранковий пуш

JobQueue, щодня о 08:00 Kyiv, кожному юзеру.

Код збирає детерміновано:

```text
today:    due_at сьогодні OR due_from <= today <= due_to
overdue:  status=open AND (due_to < today OR due_at < now)
open:     решта відкритих без дат — якщо ≤ 5, інакше "і ще N"
```

Якщо все порожньо — не писати.

LLM отримує список і формулює коротко, завершуючи "Нічого не забув?". Відповідь юзера йде через §5 і закриває або додає.

Пуш о конкретній годині — не в MVP. Якщо болить: `notified_at` у commitments + JobQueue раз на хвилину. Одна колонка, десять рядків.

---

## 7. Web view

Server-rendered HTML, без JS-фреймворку, мінімум CSS. Basic auth, логін/пароль в env — один на двох.

```text
GET  /                 — commitments: today / overdue / open; memories newest first
GET  /?q=котел         — FTS5 по memories + LIKE по commitments
POST /commitments/:id/done
POST /commitments/:id/drop
GET  /messages         — raw messages з llm_result, розгорнутий JSON (debug)
```

`done`/`drop` з web — той самий код, що `close` op з LLM. Ніякої окремої логіки.

Не робимо на web: створення, редагування, drag, фільтри. Якщо захочеться щось змінити — пишеш боту.

---

## 8. Команди

```text
/start   — привітання, перевірка allowlist
/debug   — llm_result останнього повідомлення + що застосовано
/web     — лінк на web view
```

Все інше — natural language або voice.

---

## 9. Архітектура і стек

```text
Telegram (long polling) ─┐
                         ├─→ один процес
HTTP :8080 (web view) ───┘
    ↓
handler → context builder → LLM → apply ops → reply
    ↓
SQLite (/data/family.db, WAL)
    ↑
JobQueue: ранковий пуш 08:00
```

Один процес, бот і web на одному event loop, один writer у SQLite.

### Варіант Python

```text
Python 3.12
python-telegram-bot[job-queue]   — бот, JobQueue
FastAPI + uvicorn                — web; PTB стартує у lifespan
sqlite3 stdlib + FTS5
anthropic SDK, structured output через tool_use з JSON schema
openai SDK (Whisper) або deepgram
pyyaml
```

### Варіант Node

```text
Node 22
grammY                           — бот
Hono                             — web
better-sqlite3 + FTS5
@anthropic-ai/sdk
openai (Whisper) або deepgram
node-cron                        — ранковий пуш
yaml
```

Обидва працюють. Node трохи природніший для "бот + web в одному процесі", Python має зручніший JobQueue. Вирішити до першого рядка коду.

**Модель:** Sonnet. Haiku — порівняти, коли є тестові сценарії.

**Env:**

```text
TELEGRAM_TOKEN
ANTHROPIC_API_KEY
TRANSCRIPTION_API_KEY
DATABASE_PATH       # локально ./data/family.db, на Fly /data/family.db
FAMILY_YAML
WEB_USER, WEB_PASSWORD
TZ=Europe/Kyiv
```

### Fly.io

```toml
app = "family-ea"
primary_region = "waw"

[build]
  dockerfile = "Dockerfile"

[env]
  DATABASE_PATH = "/data/family.db"
  TZ = "Europe/Kyiv"

[[mounts]]
  source = "data"
  destination = "/data"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = false      # інакше засне і бот, і cron
  auto_start_machines = true
  min_machines_running = 1

[[vm]]
  size = "shared-cpu-1x"
  memory = "256mb"
```

Бот на long polling, web на http_service. `fly volumes create data --size 1`. Секрети через `fly secrets set`. Бекапи: щоденні snapshot'и volume від Fly; Litestream у R2 — коли дані стане шкода.

---

## 10. Не робимо

- triggers, events, будь-який "агентний" runtime
- privacy / visibility
- correction через reply-to
- embeddings
- імпорт з інших систем — копі-паст у чат
- web: створення, редагування, фільтри, views
- calendar / contacts / email sync
- photo
- пуш о конкретній годині
- дитячі акаунти, multi-family
- Holmes — окремий бот, окрема база

---

## 11. Сценарії приймання

**A. Memory.** Oleh: "Газовик Петро ремонтував котел, +380…". Через тиждень: "Хто ремонтував котел?" → правильна відповідь із номером.

**B. Commitment з часом.** Anna: "Завтра о 15:30 стоматолог." → зранку в пуші "Стоматолог о 15:30".

**C. Commitment з вікном.** Oleh: "Цього або наступного тижня поговорити з пані Марією." → в пуші як "висить" усі дні вікна; після due_to — overdue.

**D. Питання по петлях.** Anna через кілька днів: "Що у нас висить по Олі?" → бот згадує Марію.

**E. Shared.** Anna: "Думаю завтра завезти мамі продукти." Oleh: "Що Анна планувала завтра?" → бот відповідає.

**F. Закриття з пушу.** Пуш → Anna: "Марію закрий, форму попрала" → обидва done, наступного ранку їх нема.

**G. Виправлення текстом.** Bot: "Записав: Марія до п'ятниці." Anna: "ні, протягом двох тижнів" → due_to оновлено, оригінал без змін.

**H. Порожній ранок.** Нічого не заплановано і не висить → пуш не приходить.

**I. Bulk paste.** Oleh вставляє 15 нотаток одним повідомленням → 15 memories/commitments, кожна видна на web окремим рядком.

**J. Web done.** Anna тисне done на "стоматолог" → у наступному пуші його нема; бот на "що у мене сьогодні" його не згадує.

**K. Voice.** Oleh наговорює про газовика → бот показує транскрипт і "Записав" → сценарій A працює далі.

---

## 12. Порядок збірки

**Зріз 1 — voice + memory + Q&A + web (read-only).** Транскрипція в пайплайн з першого дня — це основний спосіб вводу. messages, memories, context builder, один call, `/debug`, web `GET /` і `/messages`. Без JobQueue. Сценарії A, E, G (для memories), I, K.

**Зріз 2 — commitments + ранковий пуш + web done.** commitments, ops create/update/close, JobQueue 08:00, `POST done/drop`. Сценарії B, C, D, F, G, H, J.

Потім два тижні реального використання. Далі — за болем: пуш о годині, privacy, embeddings, редагування на web.

---

## 13. Відкриті рішення

- **Python чи Node.** Див. §9. Вирішити до зрізу 1.
- **Транскрипція.** Whisper через OpenAI — простіше, якщо ключ уже є. Deepgram — швидше і краще з українською, але ще один акаунт.
- **Тести без Telegram.** Сценарії §11 як JSON: вхідні повідомлення → очікувані ops. Скрипт ганяє через LLM і порівнює. Потрібно, щоб міняти промпт і модель, не ламаючи те, що працювало.
