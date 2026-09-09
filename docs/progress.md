# Прогрес і що далі

Робочий файл для продовження між сесіями. Оновлювати в кінці кожної сесії.
Спека: [spec-v3.md](spec-v3.md). Конвенції коду: [CLAUDE.md](../CLAUDE.md).

Оновлено: 2026-09-09 (пізній вечір).

## Стан

**Інфра.** Репо `imax/familyOS`, публічне, MIT. Історію скинуто до одного коміту
2026-09-09 після того, як у публічний репо потрапили справжні імена сім'ї; тепер усі імена
в репо вигадані (Олег, Анна, Оля, пані Марія, Петро). Python 3.12 + uv, ruff, pytest.
`Dockerfile` і `fly.toml` (app `familyos`, регіон `waw`, volume `data` → `/data`) готові,
але не перевірені: Docker локально нема, `fly auth login` не робили, app і volume не створені.

**Зріз 1 — зроблено.** Пайплайн повідомлення повністю: store → контекст → один
`messages.parse()` → apply ops → відповідь. Бот (allowlist, `/start` `/debug` `/web`,
текст, голос через OpenAI `gpt-4o-transcribe`). Web (basic auth, `GET /` з бакетами
today/overdue/open + memories + `?q=`, `GET /messages`). REPL без Telegram:
`uv run python -m family_ea chat --as oleh`. 17 детермінованих тестів, LLM підмінений.

Commitments (таблиця, ops create/update/close, бакети) зроблені вже в зрізі 1, бо схема
LLM-виходу їх і так містить. Зі зрізу 2 лишились ранковий пуш і web done/drop.

**Не перевірено.** Жодного живого виклику Claude і жодної транскрипції: в оточенні
розробки не було ключів. Промпт і схема писались наосліп, чекають прогону.

## Наступні кроки

1. **Живий прогін.** `.env` з ключами і `FAMILY=id:telegram_id:ім'я,...`
   (`/start` від невідомого юзера відповідає його id). Прогнати сценарії A, E, G, I, K зі
   спеки §11 через `chat --as oleh` і через бота. Дивитись `/debug`: чи правильно
   парсить дати, чи не дублює, чи закриває за id. Правити промпт у `llm.py`.
2. **Деплой.** `fly auth login` → `fly launch --no-deploy` → `fly volumes create data --size 1
   --region waw` → `fly secrets set ...` → `fly deploy`. Виставити `WEB_URL`. Перевірити,
   що `FAMILY` та інші секрети виставлені.
3. **Зріз 2.** JobQueue щодня 08:00 Kyiv кожному з family; код збирає бакети
   (`context.bucket_commitments` + `render_digest` уже є), LLM формулює коротко і завершує
   «Нічого не забув?»; порожньо → не писати. Web `POST /commitments/:id/done|drop` через
   той самий `db.close_commitment`. Сценарії B, C, D, F, H, J.
4. **Сценарні тести через LLM** (§13): сценарії як JSON (вхід → очікувані ops), скрипт
   ганяє через справжню модель і порівнює. Потрібно, щоб міняти промпт і модель
   (порівняти `claude-haiku-4-5`) без регресій.
5. Два тижні реального використання, далі за болем.

## Відкриті рішення

**People: вирішено, memories-only (2026-09-09).** Окремого файлу з людьми нема.
Код знає лише двох юзерів бота з env `FAMILY=id:telegram_id:ім'я,...` (allowlist і
відображувані імена). Хто є хто в сім'ї, вчителі, майстри, зв'язки — звичайні memories,
промпт про це знає. Якщо за два тижні виявиться, що «хто є хто» розмазується і в
контексті бракує компактного блоку, повернемось до ідеї `people.md` на volume, який
веде сама система через окрему LLM-операцію.

**Модель.** `claude-sonnet-5`, effort `medium`, обидва з env. Haiku порівняти, коли
будуть сценарні тести.

## Відхилення від спеки

- `messages.chat_with` — чий це чат; без цього відповіді бота в контексті не мають адресата.
- `messages.llm_result` зберігає `{model, usage, request_id, output, applied}`, не голий вихід.
- Commitments у зрізі 1, а не 2.
- `TRANSCRIPTION_API_KEY` → `OPENAI_API_KEY`, транскрипція через httpx, без openai SDK.
- Замість `family.yaml` (§4 спеки) — env `FAMILY` для двох юзерів, решта людей у memories.
- Fly app `familyos`, не `family-ea`.

## Дрібниці

- pyenv на цій машині без 3.12: голий `python3` у теці падає на системний 3.9 з
  попередженням. `uv run` це не зачіпає. Ліки: `pyenv install 3.12` або ігнорувати.
- FastAPI-модулі без `from __future__ import annotations` (ламає `Annotated` dependency).
- SQLite `LIKE`/`lower()` ASCII-only; для українського пошуку є `ufold()` у `db.py`.
