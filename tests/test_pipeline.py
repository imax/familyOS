import json
from pathlib import Path

from family_ea.db import Database
from family_ea.family import Family, Member
from family_ea.files import FileStore, files_for
from family_ea.llm import Image, LlmCall, LlmError, LlmResult
from family_ea.pipeline import ERROR_REPLY, Pipeline
from tests.conftest import KYIV


class FakeLlm:
    def __init__(self, result: LlmResult | None) -> None:
        self.result = result
        self.contexts: list[str] = []
        self.images: list[Image | None] = []

    async def run(self, context: str, image: Image | None = None) -> LlmCall:
        self.contexts.append(context)
        self.images.append(image)
        if self.result is None:
            raise LlmError("boom")
        return LlmCall(self.result, "fake-model", {"input_tokens": 1, "output_tokens": 1}, "req")


async def test_pipeline_happy_path(db: Database, family: Family, oleh: Member) -> None:
    llm = FakeLlm(
        LlmResult.model_validate(
            {"reply": "Записав.", "journal": [{"op": "create", "text": "Газовик Петро"}]}
        )
    )
    outcome = await Pipeline(db, family, llm, KYIV).handle(
        oleh, "Приходив газовик Петро", is_voice=True, tg_message_id=10
    )
    assert outcome.reply == "Записав."  # the transcript is not echoed back
    assert outcome.error is None and [a.ok for a in outcome.applied] == [True]
    assert "Нове повідомлення" in llm.contexts[0]

    stored = db.get_message(outcome.message_id)
    assert stored and stored.is_voice and stored.tg_message_id == 10
    result = json.loads(stored.llm_result or "")
    assert result["model"] == "fake-model"
    assert result["output"]["journal"][0]["text"] == "Газовик Петро"
    assert result["applied"][0]["kind"] == "entry"

    bot_msg = db.get_message(outcome.bot_message_id)
    assert bot_msg and bot_msg.user_id == "bot" and bot_msg.chat_with == "oleh"
    assert bot_msg.raw_text == "Записав."
    assert [e.text for e in db.list_entries()] == ["Газовик Петро"]


async def test_pipeline_llm_failure_keeps_message(
    db: Database, family: Family, oleh: Member
) -> None:
    outcome = await Pipeline(db, family, FakeLlm(None), KYIV).handle(oleh, "hi")
    assert outcome.reply == ERROR_REPLY and outcome.error
    stored = db.get_message(outcome.message_id)
    assert stored and "boom" in json.loads(stored.llm_result or "")["error"]
    assert db.get_message(outcome.bot_message_id).raw_text == ERROR_REPLY


async def test_pipeline_keeps_the_photo_as_an_attachment(
    db: Database, family: Family, oleh: Member, tmp_path: Path
) -> None:
    llm = FakeLlm(
        LlmResult.model_validate(
            {
                "reply": "Записав.",
                "journal": [{"op": "create", "text": "Чек: 1 200 грн"}],
            }
        )
    )
    store = FileStore(tmp_path / "files")
    outcome = await Pipeline(db, family, llm, KYIV, store=store).handle(
        oleh, "додай у нотатки", photo=Image(b"jpeg-bytes", "image/jpeg"), photo_file_id="tg-1"
    )
    [(a, m)] = db.attachments_with_messages()
    assert m.id == outcome.message_id and m.photo_file_id == "tg-1"
    assert (a.mime, a.size, a.name) == ("image/jpeg", 10, None)
    assert store.path(a.sha256, a.mime).read_bytes() == b"jpeg-bytes"
    [entry] = db.list_entries()
    assert [x.id for x in files_for(db, "entry", [entry])[entry.id]] == [a.id]

    # the file is stored before the model is called: a failed call loses nothing
    failed = await Pipeline(db, family, FakeLlm(None), KYIV, store=store).handle(
        oleh, "", photo=Image(b"png-bytes", "image/png")
    )
    assert failed.error and len(db.list_attachments()) == 2
    assert db.list_attachments()[1].message_id == failed.message_id

    # no store (tests): the photo is read, not kept
    await Pipeline(db, family, llm, KYIV).handle(oleh, "x", photo=Image(b"gone", "image/jpeg"))
    assert len(db.list_attachments()) == 2


async def test_pipeline_sends_the_photo_and_keeps_its_id(
    db: Database, family: Family, oleh: Member
) -> None:
    llm = FakeLlm(
        LlmResult.model_validate(
            {"reply": "Записав.", "journal": [{"op": "create", "text": "Чек: 1 200 грн"}]}
        )
    )
    photo = Image(b"\xff\xd8not-really-a-jpeg", "image/jpeg")
    outcome = await Pipeline(db, family, llm, KYIV).handle(
        oleh, "додай у нотатки", photo=photo, photo_file_id="AgACAgIAAxkBAAI", tg_message_id=11
    )
    assert outcome.reply == "Записав."
    assert llm.images == [photo]  # the bytes go to the LLM with this call only
    assert "з фото (підпис нижче):\nдодай у нотатки" in llm.contexts[0]
    stored = db.get_message(outcome.message_id)
    assert stored and stored.photo_file_id == "AgACAgIAAxkBAAI"
    assert stored.raw_text == "додай у нотатки"

    # no caption: the LLM is told so, and the message stays an empty text
    await Pipeline(db, family, llm, KYIV).handle(oleh, "", photo=photo, photo_file_id="AgAD")
    assert "з фото (підпис нижче):\n(без підпису)" in llm.contexts[1]
    # the earlier photo message shows in the recent messages with a marker, not the image
    assert "(з фото): додай у нотатки" in llm.contexts[1]
