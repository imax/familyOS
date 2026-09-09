import json

from family_ea.db import Database
from family_ea.family import Family, Member
from family_ea.llm import LlmCall, LlmError, LlmResult
from family_ea.pipeline import ERROR_REPLY, Pipeline
from tests.conftest import KYIV


class FakeLlm:
    def __init__(self, result: LlmResult | None) -> None:
        self.result = result
        self.contexts: list[str] = []

    async def run(self, context: str) -> LlmCall:
        self.contexts.append(context)
        if self.result is None:
            raise LlmError("boom")
        return LlmCall(self.result, "fake-model", {"input_tokens": 1, "output_tokens": 1}, "req")


async def test_pipeline_happy_path(db: Database, family: Family, oleh: Member) -> None:
    llm = FakeLlm(
        LlmResult.model_validate(
            {"reply": "Записав.", "memories": [{"op": "create", "text": "Газовик Петро"}]}
        )
    )
    outcome = await Pipeline(db, family, llm, KYIV).handle(
        oleh, "Приходив газовик Петро", is_voice=True, tg_message_id=10
    )
    assert outcome.reply == "🎙 «Приходив газовик Петро»\n\nЗаписав."
    assert outcome.error is None and [a.ok for a in outcome.applied] == [True]
    assert "Нове повідомлення" in llm.contexts[0]

    stored = db.get_message(outcome.message_id)
    assert stored and stored.is_voice and stored.tg_message_id == 10
    result = json.loads(stored.llm_result or "")
    assert result["model"] == "fake-model"
    assert result["output"]["memories"][0]["text"] == "Газовик Петро"
    assert result["applied"][0]["kind"] == "memory"

    bot_msg = db.get_message(outcome.bot_message_id)
    assert bot_msg and bot_msg.user_id == "bot" and bot_msg.chat_with == "oleh"
    assert bot_msg.raw_text == "Записав."  # transcript is not duplicated into the bot row
    assert [m.text for m in db.list_memories()] == ["Газовик Петро"]


async def test_pipeline_llm_failure_keeps_message(
    db: Database, family: Family, oleh: Member
) -> None:
    outcome = await Pipeline(db, family, FakeLlm(None), KYIV).handle(oleh, "hi")
    assert outcome.reply == ERROR_REPLY and outcome.error
    stored = db.get_message(outcome.message_id)
    assert stored and "boom" in json.loads(stored.llm_result or "")["error"]
    assert db.get_message(outcome.bot_message_id).raw_text == ERROR_REPLY
