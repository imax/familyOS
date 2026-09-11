"""The user turn of the one LLM call: the context alone, or a photo before it."""

import base64

from family_ea.llm import Image, user_content


def test_user_content_is_the_context_alone_without_a_photo() -> None:
    assert user_content("## Зараз\n...", None) == "## Зараз\n..."


def test_user_content_puts_the_photo_before_the_context() -> None:
    blocks = user_content("## Зараз\n...", Image(b"\x89PNG\r\n", "image/png"))
    assert isinstance(blocks, list) and [b["type"] for b in blocks] == ["image", "text"]
    source = blocks[0]["source"]
    assert source["type"] == "base64" and source["media_type"] == "image/png"
    assert base64.b64decode(source["data"]) == b"\x89PNG\r\n"
    assert blocks[1] == {"type": "text", "text": "## Зараз\n..."}
