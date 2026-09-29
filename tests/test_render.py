import re

import pytest

from sablya.core.align import normalize
from sablya.core.ffmpeg import has_filter, run, tool
from sablya.core.models import STYLES, Word
from sablya.core.render import HIGHLIGHT, burn, write_ass

DIALOGUE = re.compile(r"^Dialogue: (\d+),([\d:.]+),([\d:.]+),[^,]*,[^,]*,\d+,\d+,\d+,[^,]*,(.*)$")


def seconds(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def events(ass: str) -> list[tuple[float, float, str]]:
    out = []
    for line in ass.splitlines():
        if m := DIALOGUE.match(line):
            out.append((seconds(m[2]), seconds(m[3]), m[4]))
    return out


@pytest.mark.parametrize("style", STYLES)
def test_one_highlighted_word_per_event(tmp_path, words, style):
    path = tmp_path / "subs.ass"
    write_ass(words, (1080, 1920), style, path)
    evs = events(path.read_text(encoding="utf-8"))
    assert len(evs) == len(words)
    for _, _, text in evs:
        assert text.count(HIGHLIGHT) == 1
        if style == "single":
            visible = re.sub(r"\{[^}]*\}", "", text).split()
            assert len([t for t in visible if normalize(t)]) == 1


@pytest.mark.parametrize("style", STYLES)
def test_events_do_not_overlap(tmp_path, words, style):
    path = tmp_path / "subs.ass"
    write_ass(words, (1080, 1920), style, path)
    evs = events(path.read_text(encoding="utf-8"))
    for (s1, e1, _), (s2, _, _) in zip(evs, evs[1:]):
        assert s1 <= e1 <= s2 + 1e-9


def test_escapes_override_braces(tmp_path):
    path = tmp_path / "subs.ass"
    write_ass([Word("{\\b1}hi", 0, 1)], (100, 100), "classic", path)
    (_, _, text), = events(path.read_text(encoding="utf-8"))
    assert "\\b1" not in text


@pytest.mark.parametrize("style", STYLES)
def test_libass_renders_a_frame(tmp_path, words, style):
    if not has_filter("ass"):
        pytest.skip("ffmpeg without libass (set SABLYA_FFMPEG)")
    video = tmp_path / "in.mp4"
    run(tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=270x480:d=20:r=5",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "20", "-shortest",
        "-c:v", "libx264", "-c:a", "aac", str(video))
    ass = tmp_path / "subs.ass"
    write_ass(words, (270, 480), style, ass)
    out = tmp_path / "out.mp4"
    burn(video, ass, out, extra=["-ss", "14", "-frames:v", "1"])
    assert out.stat().st_size > 0
