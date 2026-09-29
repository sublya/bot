import json
from pathlib import Path

import pytest

from sublya.core.align import align
from sublya.core.models import Transcript, Word

FIXTURE = Path(__file__).parent / "fixtures" / "natasha"
LAG = 0.15  # whisper.cpp DTW marks tokens a bit late


@pytest.fixture
def transcript() -> Transcript:
    words = [Word(**w) for w in json.loads((FIXTURE / "words.json").read_text())]
    meta = json.loads((FIXTURE / "silences.json").read_text())
    return Transcript(
        words=words,
        silences=[tuple(p) for p in meta["silences"]],
        size=(1080, 1920),
        duration=meta["duration"],
        timing="mark",
    )


@pytest.fixture
def api_transcript() -> Transcript:
    """The same video recognised by whisper-large-v3-turbo through OpenRouter."""
    return Transcript.from_json((FIXTURE / "openrouter.json").read_text(encoding="utf-8"))


@pytest.fixture
def poem() -> str:
    return (FIXTURE / "poem.txt").read_text(encoding="utf-8")


@pytest.fixture
def words(transcript, poem) -> list[Word]:
    return align(transcript, poem, lag=LAG)
