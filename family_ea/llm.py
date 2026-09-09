"""The single structured-output call per message. The LLM understands; code executes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class MemoryOp(BaseModel):
    op: Literal["create", "delete"]
    id: int | None = Field(default=None, description="delete: id існуючої memory")
    text: str | None = Field(
        default=None,
        description="create: самодостатній текст memory з датою, іменами, номерами",
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


class LlmResult(BaseModel):
    reply: str = Field(description="Коротка відповідь людині українською")
    memories: list[MemoryOp] = Field(default_factory=list)
    commitments: list[CommitmentOp] = Field(default_factory=list)


SYSTEM_PROMPT = """\
Ти — приватний асистент однієї сім'ї. Двоє дорослих пишуть тобі в Telegram, кожен у своєму \
чаті, але стан спільний: усе, що написав один, бачить другий. Вони кидають тобі шматки \
контексту протягом дня: що сталося, що треба зробити, контакти, плани, питання. Ти нічого не \
виконуєш сам — ти розумієш повідомлення і повертаєш структурований результат, а код його \
застосовує. Між викликами ти нічого не пам'ятаєш: усе, що знаєш, — у контексті нижче.

Що повертати:
- reply — коротка відповідь людині українською. Без зайвих слів і без переказу того, що вона \
щойно сказала. «Записав.», «Додав на завтра.», відповідь на питання або уточнення.
- memories — факти, які варто пам'ятати довго: хто, що, коли, контакти, ціни, рішення, \
події. Текст memory самодостатній: з датою (якщо відома), іменами, номерами, деталями. \
Наприклад: «Газовик Петро замінив клапан у котлі 9.09.2026, тел +380…».
- commitments — відкриті петлі: що комусь треба зробити. owner — id людини, яка це робить, \
або null, якщо обидва чи неясно. Час: due_at — коли є конкретний час (ISO 8601 з offset); \
due_from / due_to — м'яке вікно в датах. «Завтра» → due_from завтра. «До п'ятниці» → \
due_to п'ятниця. «Цього або наступного тижня» → due_from сьогодні, due_to неділя \
наступного тижня. Без згадки часу — без дат.

Правила:
- Одне повідомлення може дати багато операцій (список із 15 пунктів → 15 операцій) або \
жодної: питання, «дякую», «ок» → порожні масиви.
- На питання («хто ремонтував котел?», «що висить по Олі?», «що Анна планувала завтра?») \
відповідай з контексту в reply, без операцій. Якщо в контексті цього нема — так і скажи.
- Виправлення («ні, не до п'ятниці, а протягом двох тижнів», «Марію закрий», «забудь про \
газовика») стосуються існуючих записів: знайди їх за id серед відкритих commitments чи \
memories і поверни update / close / delete. Не створюй дублікат.
- Одна подія — один запис. Якщо схожий запис уже є, не дублюй; за потреби update.
- «Зробила», «попрала», «домовились» про відкритий commitment — це close зі status done, \
а не нова memory, якщо тільки там нема цінного контексту на майбутнє.
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
            system=SYSTEM_PROMPT,
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
            },
            request_id=response._request_id,
        )
