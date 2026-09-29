import json
from pathlib import Path

import pytest

from sablya.core.align import align, paginate, read_text, snap
from sablya.core.models import Transcript, Word

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
    )


@pytest.fixture
def poem() -> str:
    return (FIXTURE / "poem.txt").read_text(encoding="utf-8")


def test_snap_moves_word_start_to_pause_end(transcript):
    words = snap(transcript.words, transcript.silences, transcript.duration, LAG)
    ya = [w for w in words if w.text == "Я"]
    assert ya[1].start == pytest.approx(13.97, abs=0.01)


def test_snap_does_not_mutate_input(transcript):
    before = [(w.start, w.end) for w in transcript.words]
    snap(transcript.words, transcript.silences, transcript.duration, LAG)
    assert [(w.start, w.end) for w in transcript.words] == before


def test_align_uses_reference_wording(transcript, poem):
    words = align(transcript, poem, lag=LAG)
    assert [w.text for w in words] == [w.text for w in read_text(poem)]


def test_align_drops_extra_heard_word(transcript, poem):
    words = align(transcript, poem, lag=LAG)
    i = next(i for i, w in enumerate(words) if w.text == "Чтобы" and words[i + 1].text == "добрым")
    assert words[i - 1].text != "и"


def test_align_keeps_timing_of_replaced_word(transcript, poem):
    heard = snap(transcript.words, transcript.silences, transcript.duration, LAG)
    heard_chtoby = [w for w in heard if w.text.rstrip(",").lower() == "чтобы"][1]
    chtob = next(w for w in align(transcript, poem, lag=LAG) if w.text == "чтоб")
    assert (chtob.start, chtob.end) == pytest.approx((heard_chtoby.start, heard_chtoby.end))


def test_align_gives_short_words_nonzero_time(transcript, poem):
    words = align(transcript, poem, lag=LAG)
    i = next(i for i, w in enumerate(words) if w.text == "И" and words[i + 1].text == "о")
    assert words[i].end > words[i].start
    assert words[i + 1].end > words[i + 1].start


def test_align_marks_line_ends(transcript, poem):
    words = align(transcript, poem, lag=LAG)
    lines = [line.split() for line in poem.splitlines() if line.strip()]
    ends = [w.text for w in words if w.line_end]
    assert len(ends) == len(lines)
    assert ends == [line[-1] if line[-1] != "—" else f"{line[-2]} —" for line in lines]


def test_align_without_text_keeps_heard_words(transcript):
    words = align(transcript, None, lag=LAG)
    assert [w.text for w in words] == [w.text for w in transcript.words]


def test_read_text_glues_lone_dash():
    words = read_text("То летит не кто-нибудь —\nЭто")
    assert [w.text for w in words] == ["То", "летит", "не", "кто-нибудь —", "Это"]
    assert words[3].line_end and not words[2].line_end


def test_paginate_respects_limits_and_lines(transcript, poem):
    pages = paginate(align(transcript, poem, lag=LAG), per_page=3, max_chars=24)
    for page in pages:
        assert len(page) <= 3
        assert len(" ".join(w.text for w in page)) <= 24
        assert not any(w.line_end for w in page[:-1]), "a page crosses a poem line"


def test_transcript_json_roundtrip(transcript):
    restored = Transcript.from_json(transcript.to_json())
    assert restored == transcript
