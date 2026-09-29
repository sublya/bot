import httpx
import pytest

from sablya.core.transcribe import NoSpeech, SttConfig, SttError, recognize

CFG = SttConfig(url="https://stt.test/v1", key="k", model="whisper-x")
RESPONSE = {
    "text": "Я хочу, чтобы детей были взрослые достойны.",
    "duration": 5.0,
    "words": [
        {"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
        for i, w in enumerate("Я хочу чтобы детей были взрослые достойны".split())
    ],
}


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.wav"
    path.write_bytes(b"RIFF")
    return path


async def no_sleep(_: float) -> None:
    pass


def client(*responses: httpx.Response, seen: list | None = None) -> httpx.AsyncClient:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return queue.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_parses_words_and_restores_punctuation(audio):
    seen = []
    words = await recognize(audio, CFG, client=client(httpx.Response(200, json=RESPONSE), seen=seen))
    assert [w.text for w in words] == "Я хочу, чтобы детей были взрослые достойны.".split()
    assert (words[1].start, words[1].end) == (0.5, 0.9)
    req = seen[0]
    assert str(req.url) == "https://stt.test/v1/audio/transcriptions"
    assert req.headers["authorization"] == "Bearer k"
    body = req.content.decode(errors="replace")
    assert 'name="model"\r\n\r\nwhisper-x' in body
    assert 'name="timestamp_granularities[]"\r\n\r\nword' in body
    assert 'name="language"' not in body


async def test_sends_language_when_given(audio):
    seen = []
    await recognize(audio, CFG, lang="ru", client=client(httpx.Response(200, json=RESPONSE), seen=seen))
    assert 'name="language"\r\n\r\nru' in seen[0].content.decode(errors="replace")


async def test_retries_after_server_error(audio):
    c = client(httpx.Response(503), httpx.Response(429), httpx.Response(200, json=RESPONSE))
    words = await recognize(audio, CFG, client=c, sleep=no_sleep)
    assert len(words) == 7


async def test_gives_up_after_retries(audio):
    c = client(*[httpx.Response(500)] * 4)
    with pytest.raises(SttError):
        await recognize(audio, CFG, client=c, sleep=no_sleep)


async def test_does_not_retry_client_error(audio):
    c = client(httpx.Response(401), httpx.Response(200, json=RESPONSE))
    with pytest.raises(SttError):
        await recognize(audio, CFG, client=c, sleep=no_sleep)


async def test_no_speech(audio):
    c = client(httpx.Response(200, json={"text": "", "words": []}))
    with pytest.raises(NoSpeech):
        await recognize(audio, CFG, client=c)


async def test_missing_word_timestamps_is_an_error(audio):
    c = client(httpx.Response(200, json={"text": "Я хочу"}))
    with pytest.raises(SttError, match="word"):
        await recognize(audio, CFG, client=c)
