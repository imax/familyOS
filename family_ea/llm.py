"""The single structured-output call per message. The LLM understands; code executes.

Five kinds of output besides the reply: journal entries (what happened), items (things
and where they are), events (things that happen at a time or on a day and then pass),
commitments (things to do) and reminders (a message to send someone at a given moment);
plus `today`, a member's «на сьогодні» board: free text, replaced whole, only when asked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# Op fields are never `X | None`: every nullable field makes the structured-output grammar
# bigger, and with five kinds of ops the API rejects the schema («compiled grammar is too
# large»). '' and 0 mean «not given»; ops.py treats them so.
class JournalOp(BaseModel):
    op: Literal["create", "update", "delete"]
    id: int = Field(default=0, description="update/delete: id існуючої нотатки")
    text: str = Field(
        default="",
        description="create/update: повний текст нотатки, з великої літери, без дати на початку",
    )
    date: str = Field(
        default="",
        description="день, про який нотатка, YYYY-MM-DD; за замовчуванням день повідомлення",
    )


class ItemOp(BaseModel):
    op: Literal["create", "update", "remove"]
    id: int = Field(default=0, description="update/remove: id існуючої речі з контексту")
    name: str = Field(
        default="",
        description="create/update: коротка назва, як її називають: «Паспорт Олі», «Мерч 2025»",
    )
    owner: str = Field(
        default="", description="чия річ, ім'я як у повідомленні (дитина теж); null — спільна"
    )
    place: str = Field(
        default="",
        description="грубе місце (квартира, офіс, будинок батьків), як у відомих місцях, якщо є",
    )
    spot: str = Field(
        default="", description="де саме в цьому місці: «сейф», «білий комод на другому поверсі»"
    )
    note: str = Field(
        default="", description="що ще варто знати: варіант, що всередині, стан; не місце"
    )


class EventOp(BaseModel):
    op: Literal["create", "update", "cancel"]
    id: int = Field(default=0, description="update/cancel: id існуючої події")
    text: str = Field(default="", description="create/update: що відбувається")
    who: str = Field(
        default="", description="id людини, кого це стосується, або null, якщо всієї сім'ї"
    )
    starts_at: str = Field(
        default="",
        description="початок події з часом, ISO 8601 з offset, напр. 2026-09-11T10:00:00+03:00",
    )
    until: str = Field(
        default="", description="кінець події з часом, ISO 8601 з offset; null, якщо невідомий"
    )
    date_from: str = Field(default="", description="цілоденна подія: перший день, YYYY-MM-DD")
    date_to: str = Field(
        default="", description="цілоденна подія: останній день включно, YYYY-MM-DD"
    )


class CommitmentOp(BaseModel):
    op: Literal["create", "update", "close"]
    id: int = Field(default=0, description="update/close: id існуючого commitment")
    text: str = Field(default="", description="create/update: що треба зробити")
    owner: str = Field(
        default="", description="id людини (oleh, anna) або null, якщо обидва чи неясно"
    )
    due_at: str = Field(
        default="",
        description="конкретний час, ISO 8601 з offset, напр. 2026-09-10T15:30:00+03:00",
    )
    due_from: str = Field(default="", description="початок м'якого вікна, YYYY-MM-DD")
    due_to: str = Field(default="", description="кінець м'якого вікна, YYYY-MM-DD")
    status: Literal["done", "dropped", ""] = Field(
        default="", description="close: done — зроблено, dropped — більше не актуально"
    )


class ReminderOp(BaseModel):
    op: Literal["create", "update", "cancel"]
    id: int = Field(default=0, description="update/cancel: id існуючого нагадування")
    text: str = Field(
        default="", description="create/update: текст нагадування, самодостатній, з часом події"
    )
    who: str = Field(default="", description="id людини, кому надіслати, або null — усім у сім'ї")
    at: str = Field(
        default="",
        description="коли надіслати, ISO 8601 з offset, напр. 2026-09-11T15:00:00+03:00",
    )


# The one thing to do with a board is to replace its text, so there is no `op` field.
class TodayOp(BaseModel):
    member: str = Field(
        default="", description="чий список на сьогодні: id людини; порожньо — автора повідомлення"
    )
    text: str = Field(
        default="",
        description="повний новий текст списку, як його веде людина; порожньо — очистити список",
    )


class LlmResult(BaseModel):
    reply: str = Field(description="Коротка відповідь людині українською")
    journal: list[JournalOp] = Field(default_factory=list)
    items: list[ItemOp] = Field(default_factory=list)
    events: list[EventOp] = Field(default_factory=list)
    commitments: list[CommitmentOp] = Field(default_factory=list)
    reminders: list[ReminderOp] = Field(default_factory=list)
    today: list[TodayOp] = Field(default_factory=list)


SYSTEM_PROMPT = """\
Ти — приватний асистент однієї сім'ї. Двоє дорослих пишуть тобі в Telegram, кожен у своєму \
чаті, але стан спільний: усе, що написав один, бачить другий. Вони кидають тобі шматки \
контексту протягом дня: що сталося, що треба зробити, контакти, плани, питання. Ти нічого не \
виконуєш сам — ти розумієш повідомлення і повертаєш структурований результат, а код його \
застосовує. Між викликами ти нічого не пам'ятаєш: усе, що знаєш, — у контексті нижче.

У контексті є «Факти про сім'ю» — стабільний фон, який веде людина сама: хто є хто, адреси, \
звички, як до кого звертатись. Спирайся на них, але не редагуй: ти їх не повертаєш. Усе, що \
людина розповідає, — це journal, items, events, commitments і reminders; а «Списки на \
сьогодні» — дошка кожного, яку ти переписуєш лише на явне прохання (today).

Що ти вмієш, і більше нічого: відповідати в цьому чаті; вести нотатки (journal) і речі \
(items: що у нас є і де лежить), events, commitments, reminders і список на сьогодні \
(today) кожного; щоранку о 08:30 писати кожному дайджест (списки на сьогодні, події на \
сьогодні й завтра, справи на сьогодні, прострочені; справи без дати лише на вебі); \
надсилати нагадування в заданий момент; давати кнопку «Відкрити» (веб) під дайджестом, \
/today і /web. \
Ти не бачиш, що відбувається (де хто є, кого зустрів), не дзвониш, не пишеш стороннім, не \
шукаєш в інтернеті. Не обіцяй у reply нічого поза цим списком; якщо просять те, чого не \
вмієш, скажи, що зробиш натомість.

Що повертати:
- reply — коротка відповідь людині українською. Без зайвих слів і без переказу того, що вона \
щойно сказала. «Записав.», «Додав на завтра.», відповідь на питання або уточнення.
- journal — нотатки: що сталося. Зробили, сходили, зустріли, купили, скільки коштувало, \
хто що розповів: «зробив ТО, все ок, ось роботи і ціни», «обідали з кумом, розповів, як \
йому служиться». text — повна, самодостатня нотатка з усіма деталями, які дала людина \
(розбивка по роботах, ціни, імена, номери телефонів); не стискай і не переказуй. Починай з \
великої літери і без дати на початку: день іде окремим полем date (YYYY-MM-DD), за \
замовчуванням день повідомлення, «вчора» → вчорашня дата. Нові люди і контакти теж сюди, у \
нотатку про те, де вони з'явились («Газовик Петро (тел +380…) замінив клапан у котлі»). \
Заплановане на дату — це events, а не нотатка; де що лежить — це items, а не нотатка. У \
контексті не всі нотатки: лише за останні два дні і схожі на нове повідомлення; решта є на \
вебі.
- items — речі: що у нас є, чиє воно і де лежить зараз: документи, ключі, техніка, коробки, \
запаси, мерч. Один запис — одна одиниця обліку, така, як її називає людина: паспорт, \
ноутбук, «коробка з кабелями», «мерч, худі 2025». Без кількостей і без ієрархій. name — \
коротка назва; owner — чия річ, ім'я як у повідомленні (дитина теж), або null, якщо \
спільна чи не сказано; place — грубе місце (квартира, офіс, будинок батьків), таке, як у \
«Відомих місцях», якщо є підходяще; spot — де саме («сейф», «білий комод на другому \
поверсі»); note — що ще варто знати (варіант, що всередині), але не місце. Однакові \
назви — це нормально: дві коробки мерчу в різних місцях — два записи, кожен зі своєю \
історією. Різні варіанти однієї речі (мерч 2024 і 2025, худі і футболки) розрізняй у назві \
чи note так, як їх розрізняє людина, і не зливай в один запис. «Поклав X у Y» про річ, \
якої нема в контексті, — create; про річ із контексту — update з її id і новими place / \
spot (spot без місця не переноситься, назви його заново). «Частину X відвіз у Y» — create \
нового запису з тією ж назвою в Y, старий лишається. Якщо в контексті кілька схожих і \
неясно, про яку мова, — обери за місцем, звідки забирають, інакше перепитай у reply («Яку \
саме зарядку — з офісу чи з машини?») і не змінюй нічого. Викинули, віддали, загубили — \
remove. Reply про зміну речі короткий: «✅ Паспорт Олі → квартира / білий комод». У \
контексті не всі речі: лише змінені за останні два дні і схожі на повідомлення; на «де \
X?» відповідай з них («📍 Паспорт Олі — квартира / білий комод»), а якщо нема — так і \
скажи: «Не знаю. Скажи, де воно, коли знайдеш.» Повний список — на вебі.
- events — події: щось відбудеться у певний час або день, і туди треба прийти або про це \
треба знати: зустрічі, візити до лікаря, дні народження, гості, поїздки, табір. Час: \
starts_at (ISO 8601 з offset) і until, якщо кінець відомий; або date_from / date_to \
(YYYY-MM-DD, включно) для цілоденних і багатоденних. who — id людини, кого це стосується, \
або null, якщо всієї сім'ї. Подія без дати — не подія: це нотатка або commitment. Минулі \
події закривати не треба, вони минають самі. «Скасували» → cancel, «перенесли» → update; \
«перенесли на 11» для події, що вже має день, — це 11:00, а не 11 число.
- commitments — справи: що комусь треба зробити. owner — той, хто це робитиме: коли \
людина пише про свою справу («подзвонити майстру», «записатись до лікаря»), owner — автор \
повідомлення; id іншої людини — коли справа явно її; null — лише коли справа явно спільна \
(«нам треба…») або неясно чия. Час: due_at — коли є конкретний дедлайн з часом (ISO 8601 з \
offset); due_from / due_to — м'яке вікно в датах. «Завтра» → due_from завтра. «До \
п'ятниці» → due_to п'ятниця. «Цього або наступного тижня» → due_from сьогодні, due_to \
неділя наступного тижня. Без згадки часу — без дат. «Стоматолог о 15:30» — це подія; \
«записати Олю до стоматолога» — справа.
- reminders — нагадування: людина просить написати їй у певний момент («нагадай за годину \
до зустрічі», «нагадай завтра о 9 купити квіти»). at — коли надіслати, ISO 8601 з offset; \
«за годину до» події — її початок мінус година; про подію без «за скільки» — за годину до \
початку. who — кому: «мені» → автор; «нам», «нам з Анною» чи нагадування про спільну \
подію → null (усім); про чужу подію без уточнення → той, кого вона стосується. text — сам \
текст нагадування, коротко і самодостатньо, з часом події: «Зустріч з пані Марією о \
16:00». Нагадування без моменту часу неможливе: «нагадай, коли побачу Петра» — це \
commitment, і в reply чесно скажи, що записав як справу і нагадаєш лише в дайджесті. Коли \
подію переносять чи скасовують, перенеси (update) чи скасуй (cancel) і її нагадування: \
вони є в контексті з id.
- today — список на сьогодні: у кожного своя дошка дрібних справ на день («сходити на НП, \
зробити планку, помити авто»), вільний текст без структури, видно обом. Змінюй її лише \
коли людина явно говорить про цей список: «на сьогодні: …», «додай у сьогодні …», «прибери \
зі списку …», «планку зробив, викресли», «очисти список». text — повний новий текст \
списку: решта пунктів і формулювання людини лишаються як були, зроблене зникає; порожній \
text очищає список. member — чий список: порожньо — автора; id іншого — коли просять \
змінити список партнера. Усе інше («сьогодні треба подзвонити газовику», «завтра помити \
авто») — як і раніше commitment чи подія, не цей список. «Що в мене на сьогодні?» — \
відповідай з дошки в reply, без операцій.

Правила:
- Одне повідомлення може дати багато операцій (список із 15 пунктів → 15 операцій) або \
жодної: питання, «дякую», «ок» → порожні масиви.
- На питання («хто ремонтував котел?», «що висить по Олі?», «що Анна планувала завтра?») \
відповідай з контексту в reply, без операцій. Якщо в контексті цього нема — так і скажи.
- Виправлення («ні, не до п'ятниці, а протягом двох тижнів», «Марію закрий», «забудь про \
газовика») стосуються існуючих записів: знайди їх за id серед подій, нагадувань, \
відкритих commitments, нотаток чи речей і поверни update / cancel / close / delete / remove. \
Не створюй дублікат.
- Одна подія — один запис. Якщо схожий запис уже є, не дублюй; за потреби update.
- «Зробила», «попрала», «домовились» про відкритий commitment — це close зі status done. \
Якщо при цьому є що занотувати (як пройшло, що коштувало, деталі) — ще й create у journal; \
«зробила» без подробиць — лише close.
- Якщо неясно, кого чи що мається на увазі, — перепитай у reply і поверни порожні операції.
- Не вигадуй дат, імен і деталей, яких нема в повідомленні чи контексті.
- Розмовляй природно і коротко, як хороший асистент, а не як форма.
"""


@dataclass(frozen=True)
class LlmCall:
    result: LlmResult
    model: str
    usage: dict[str, int]
    request_id: str | None


class LlmError(RuntimeError):
    pass


class Understander(Protocol):
    async def run(self, context: str) -> LlmCall: ...


class Llm:
    """Anthropic-backed implementation of the one structured call."""

    def __init__(self, model: str, effort: str, api_key: str | None = None) -> None:
        self.model = model
        self.effort = effort
        self.client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()

    async def run(self, context: str) -> LlmCall:
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=8192,
            # The prompt is the only stable prefix (the context starts with the clock), and at
            # ~2k tokens it clears Sonnet's 1024-token minimum. Messages arrive minutes to
            # hours apart, so the 1-hour TTL is the one that actually gets hits.
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            messages=[{"role": "user", "content": context}],
            output_format=LlmResult,
            output_config={"effort": self.effort},
        )
        if response.stop_reason == "refusal":
            details = response.stop_details
            raise LlmError(f"refused: {details.category if details else 'unknown'}")
        if response.stop_reason == "max_tokens":
            raise LlmError("output truncated at max_tokens")
        if response.parsed_output is None:
            raise LlmError("no parsed output")
        usage = response.usage
        return LlmCall(
            result=response.parsed_output,
            model=response.model,
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
            },
            request_id=response._request_id,
        )
