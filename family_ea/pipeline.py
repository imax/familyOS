"""One message end to end: store -> context -> LLM -> apply ops -> reply. Spec section 5."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .context import build_context
from .db import Database, Member
from .family import Family
from .llm import LlmResult, Understander
from .ops import Applied, apply_ops

log = logging.getLogger(__name__)

ERROR_REPLY = "Щось пішло не так, спробуй ще раз."


@dataclass(frozen=True)
class Outcome:
    message_id: int
    bot_message_id: int
    reply: str  # what to send; a voice transcript is not echoed (it is on /messages and /debug)
    result: LlmResult | None
    applied: list[Applied]
    error: str | None = None


class Pipeline:
    def __init__(self, db: Database, family: Family, llm: Understander, tz: ZoneInfo) -> None:
        self.db = db
        self.family = family
        self.llm = llm
        self.tz = tz

    async def handle(
        self,
        author: Member,
        text: str,
        *,
        is_voice: bool = False,
        tg_message_id: int | None = None,
    ) -> Outcome:
        message_id = self.db.insert_message(
            author.id, author.id, text, is_voice=is_voice, tg_message_id=tg_message_id
        )
        now = datetime.now(self.tz)
        context = build_context(self.db, self.family, now, author, text)

        try:
            call = await self.llm.run(context)
        except Exception as exc:  # any LLM failure: log, tell the user, keep the message
            log.exception("LLM call failed for message %s", message_id)
            self.db.set_llm_result(message_id, json.dumps({"error": repr(exc)}, ensure_ascii=False))
            bot_message_id = self.db.insert_message("bot", author.id, ERROR_REPLY)
            return Outcome(message_id, bot_message_id, ERROR_REPLY, None, [], error=repr(exc))

        applied = apply_ops(
            self.db,
            call.result,
            author_id=author.id,
            message_id=message_id,
            family=self.family,
            tz=self.tz,
        )
        self.db.set_llm_result(
            message_id,
            json.dumps(
                {
                    "model": call.model,
                    "usage": call.usage,
                    "request_id": call.request_id,
                    "output": call.result.model_dump(exclude_none=True),
                    "applied": [a.as_dict() for a in applied],
                },
                ensure_ascii=False,
            ),
        )

        reply = call.result.reply.strip() or "Ок."
        bot_message_id = self.db.insert_message("bot", author.id, reply)
        return Outcome(message_id, bot_message_id, reply, call.result, applied)
