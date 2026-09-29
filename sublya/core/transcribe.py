"""Speech recognition through any OpenAI-compatible audio/transcriptions endpoint."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .align import apply_text, normalize, read_text
from .audio import duration, extract_audio, silences, video_size
from .models import Transcript, Word

DEFAULT_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/whisper-large-v3-turbo"
BACKOFF = (1, 2, 4)
TIMEOUT = httpx.Timeout(120, connect=10)


class SttError(RuntimeError):
    pass


class NoSpeech(SttError):
    pass


@dataclass(frozen=True)
class SttConfig:
    url: str = DEFAULT_URL
    key: str = ""
    model: str = DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> "SttConfig":
        return cls(
            url=os.environ.get("STT_API_URL") or DEFAULT_URL,
            key=os.environ.get("STT_API_KEY", ""),
            model=os.environ.get("STT_MODEL") or DEFAULT_MODEL,
        )


# Whisper learned from subtitle files, and over silence or noise at the start or end of a
# recording it writes their credits. Nobody says these in a video, unlike "thank you".
HALLUCINATIONS = [
    phrase.split()
    for phrase in (
        "продолжение следует",
        "субтитры сделал dimatorzok",
        "субтитры создавал dimatorzok",
        "субтитры подогнал симон",
        "редактор субтитров асемкин корректор аегорова",
    )
]


def drop_hallucinations(words: list[Word]) -> list[Word]:
    keys = [normalize(w.text) for w in words]
    start, end = 0, len(words)
    trimmed = True
    while trimmed:
        trimmed = False
        for phrase in HALLUCINATIONS:
            n = len(phrase)
            if end - start >= n and keys[start:start + n] == phrase:
                start, trimmed = start + n, True
            if end - start >= n and keys[end - n:end] == phrase:
                end, trimmed = end - n, True
    return words[start:end]


def parse(data: dict) -> list[Word]:
    if "words" not in data:
        raise SttError("the STT response has no word timestamps")
    heard = [
        Word(w["word"].strip(), float(w["start"]), float(w["end"]))
        for w in data["words"]
        if w["word"].strip()
    ]
    # words come without punctuation; the full text has it
    ref = read_text(data.get("text", ""))
    if ref:
        ref[-1].line_end = False
        heard = apply_text(heard, ref)
    heard = drop_hallucinations(heard)
    if not heard:
        raise NoSpeech("no speech recognised")
    return heard


async def post(
    client: httpx.AsyncClient, audio: Path, cfg: SttConfig, lang: str | None,
    sleep: Callable[[float], Awaitable[None]],
) -> dict:
    form = {"model": cfg.model, "response_format": "verbose_json",
            "timestamp_granularities[]": "word"}
    if lang:
        form["language"] = lang
    content = audio.read_bytes()
    for attempt in range(len(BACKOFF) + 1):
        retry = attempt < len(BACKOFF)
        try:
            resp = await client.post(
                f"{cfg.url.rstrip('/')}/audio/transcriptions",
                headers={"Authorization": f"Bearer {cfg.key}"},
                data=form,
                files={"file": (audio.name, content, "audio/wav")},
                timeout=TIMEOUT,
            )
        except httpx.TransportError as e:
            if not retry:
                raise SttError(f"STT request failed: {e!r}") from e
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                if not retry:
                    raise SttError(f"STT returned {resp.status_code}: {resp.text[:300]}")
            elif resp.is_error:
                raise SttError(f"STT returned {resp.status_code}: {resp.text[:300]}")
            else:
                return resp.json()
        await sleep(BACKOFF[attempt])
    raise AssertionError("unreachable")


async def recognize(
    audio: Path, cfg: SttConfig, lang: str | None = None,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[Word]:
    if client is not None:
        return parse(await post(client, audio, cfg, lang, sleep))
    async with httpx.AsyncClient() as own:
        return parse(await post(own, audio, cfg, lang, sleep))


async def transcribe(
    video: Path, workdir: Path, cfg: SttConfig, lang: str | None = None
) -> Transcript:
    audio = workdir / "audio.wav"
    await asyncio.to_thread(extract_audio, video, audio)
    words = await recognize(audio, cfg, lang)
    return Transcript(
        words=words,
        silences=await asyncio.to_thread(silences, audio),
        size=await asyncio.to_thread(video_size, video),
        duration=await asyncio.to_thread(duration, audio),
        lang=lang,
    )
