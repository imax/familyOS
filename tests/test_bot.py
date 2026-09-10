from datetime import UTC, datetime

from telegram import Chat, Message, Update, User

from family_ea.bot import build_bot, family_filter
from family_ea.db import Database
from family_ea.family import Family
from tests.test_web import _settings


def _update(user_id: int) -> Update:
    user = User(id=user_id, first_name="X", is_bot=False)
    chat = Chat(id=user_id, type="private")
    msg = Message(message_id=1, date=datetime.now(UTC), chat=chat, from_user=user, text="hi")
    return Update(update_id=1, message=msg)


def test_family_filter_is_live(db: Database) -> None:
    fam = Family(db, admin_telegram_id=1)
    fam.add("Анна", 2)
    allowed = family_filter(fam)
    assert allowed.check_update(_update(1))  # admin, before their row exists
    assert allowed.check_update(_update(2))
    assert not allowed.check_update(_update(3))
    assert (~allowed).check_update(_update(3))
    fam.add("Оля", 3)  # added on the web while the bot runs
    assert allowed.check_update(_update(3))


def test_build_bot_registers_handlers(db: Database, family: Family) -> None:
    app = build_bot(_settings(telegram_token="123:abc"), family, db, None, None)  # type: ignore[arg-type]
    assert len(app.handlers[0]) == 7
