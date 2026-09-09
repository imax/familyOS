"""Voice transcription via OpenAI's audio API. The only non-Anthropic model call."""

from __future__ import annotations

import httpx

OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"


class Transcriber:
    def __init__(
        self, api_key: str, model: str = "gpt-4o-transcribe", language: str = "uk"
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.language = language

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            response = await client.post(
                OPENAI_TRANSCRIPTIONS_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (filename, audio, "audio/ogg")},
                data={"model": self.model, "language": self.language},
            )
        response.raise_for_status()
        return str(response.json().get("text", "")).strip()
