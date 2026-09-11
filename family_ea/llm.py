"""The single structured-output call per message. The LLM understands; code executes.

Four kinds of output besides the reply: journal entries (what happened), events (things
that happen at a time or on a day and then pass), commitments (things to do) and
reminders (a message to send someone at a given moment).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class JournalOp(BaseModel):
    op: Literal["create", "update", "delete"]
    id: int | None = Field(default=None, description="update/delete: id існуючої нотатки")
    text: str | None = Field(
        default=None,
        description="create/update: повний текст нотатки, з великої літери, без дати на початку",
    )
    date: str | None = Field(
        default=None,
        description="день, про який нотатка, YYYY-MM-DD; за замовчуванням день повідомлення",
    )


class EventOp(BaseModel):
    op: Literal["create", "update", "cancel"]
    id: int | None = Field(default=None, description="update/cancel: id існуючої події")
    text: str | None = Field(default=None, description="create/update: що відбувається")
    who: str | None = Field(
        default=None, description="id людини, кого це стосується, або null, якщо всієї сім'ї"
    )
    starts_at: str | None = Field(
        default=None,
        description="початок події з часом, ISO 8601 з offset, напр. 2026-09-11T10:00:00+03:00",
    )
    until: str | None = Field(
        default=None, description="кінець події з часом, ISO 8601 з offset; null, якщо невідомий"
    )
    date_from: str | None = Field(
        default=None, description="цілоденна подія: перший день, YYYY-MM-DD"
    )
    date_to: str | None = Field(
        default=None, description="цілоденна подія: останній день включно, YYYY-MM-DD"
    )


class CommitmentOp(BaseModel):
    op: Literal["create", "update", "close"]
    id: int | None = Field(default=None, description="update/close: id існуючого commitment")
    text: str | None = Field(default=None, description="create/update: що треба зробити")
    owner: str | None = Field(
        default=None, description="id людини (oleh, anna) або null, якщо обидва чи неясно"
    )
    due_at: str | None = Field(
        default=None,
        description="конкретний час, ISO 8601 з offset, напр. 2026-09-10T15:30:00+03:00",
    )
    due_from: str | None = Field(default=None, description="початок м'якого вікна, YYYY-MM-DD")
    due_to: str | None = Field(default=None, description="кінець м'якого вікна, YYYY-MM-DD")
    status: Literal["done", "dropped"] | None = Field(
        default=None, description="close: done — зроблено, dropped — більше не актуально"
    )


class ReminderOp(BaseModel):
    op: Literal["create", "update", "cancel"]
    id: int | None = Field(default=None, description="update/cancel: id існуючого нагадування")
    text: str | None = Field(
        default=None, description="create/update: текст нагадування, самодостатній, з часом події"
    )
    who: str | None = Field(
        default=None, description="id людини, кому надіслати, або null — усім у сім'ї"
    )
    at: str | None = Field(
        default=None,
        description="коли надіслати, ISO 8601 з offset, напр. 2026-09-11T15:00:00+03:00",
    )


class LlmResult(BaseModel):
    reply: str = Field(description="Коротка відповідь людині українською")
    journal: list[JournalOp] = Field(default_factory=list)
    events: list[EventOp] = Field(default_factory=list)
    commitments: list[CommitmentOp] = Field(default_factory=list)
    reminders: list[ReminderOp] = Field(default_factory=list)


SYSTEM_PROMPT = """\
Ти — приватний асистент однієї сім'ї. Двоє дорослих пишуть тобі в Telegram, кожен у своєму \
чаті, але стан спільний: усе, що написав один, бачить другий. Вони кидають тобі шматки \
контексту протягом дня: що сталося, що треба зробити, контакти, плани, питання. Ти нічого не \
виконуєш сам — ти розумієш повідомлення і повертаєш структурований результат, а код його \
застосовує. Між викликами ти нічого не пам'ятаєш: усе, що знаєш, — у контексті нижче.

У контексті є «Факти про сім'ю» — стабільний фон, який веде людина сама: хто є хто, адреси, \
звички, як до кого звертатись. Спирайся на них, але не редагуй: ти їх не повертаєш. Усе, що \
людина розповідає, — це journal, events, commitments і reminders.

Що ти вмієш, і більше нічого: відповідати в цьому чаті; вести нотатки (journal), events, \
commitments і reminders; щоранку о 08:30 писати кожному дайджест (події на сьогодні й \
завтра, справи на сьогодні, прострочені, по понеділках ще й без дати); надсилати \
нагадування в заданий момент; давати кнопку «📅», щоб додати подію в календар телефону. \
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
Заплановане на дату — це events, а не нотатка. У контексті не всі нотатки: лише за останні \
два дні і схожі на нове повідомлення; решта є на вебі.
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

Правила:
- Одне повідомлення може дати багато операцій (список із 15 пунктів → 15 операцій) або \
жодної: питання, «дякую», «ок» → порожні масиви.
- На питання («хто ремонтував котел?», «що висить по Олі?», «що Анна планувала завтра?») \
відповідай з контексту в reply, без операцій. Якщо в контексті цього нема — так і скажи.
- Виправлення («ні, не до п'ятниці, а протягом двох тижнів», «Марію закрий», «забудь про \
газовика») стосуються існуючих записів: знайди їх за id серед подій, нагадувань, \
відкритих commitments чи нотаток і поверни update / cancel / close / delete. Не створюй \
дублікат.
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
