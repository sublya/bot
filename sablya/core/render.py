"""ASS subtitles with one event per word, and burning them into the video."""

import os
from dataclasses import dataclass
from pathlib import Path

from .align import MAX_CHARS, PER_PAGE, paginate
from .ffmpeg import run, tool
from .models import DEFAULT_STYLE, Word

TAIL = 0.5  # how long the last word lingers when nothing follows it
FONT = "Montserrat ExtraBold"
HIGHLIGHT = r"{\rHi}"  # switches to the Hi style; {\r} switches back

# ASS colours are &HAABBGGRR
WHITE = "&H00FFFFFF"
BLACK = "&H00000000"
YELLOW = "&H0000FFFF"
PINK = "&H00552CFE"
SHADOW = "&H80000000"


@dataclass(frozen=True)
class Preset:
    font: float  # font size as a share of frame width
    margin: float  # bottom margin as a share of frame height
    alignment: int = 2  # numpad layout: 2 is bottom centre, 5 is middle centre
    per_page: int = PER_PAGE
    max_chars: int = MAX_CHARS
    hi_colour: str = YELLOW
    box: bool = False


PRESETS = {
    "classic": Preset(font=0.065, margin=0.14),
    "big": Preset(font=0.09, margin=0.25, max_chars=18),
    "box": Preset(font=0.065, margin=0.14, hi_colour=WHITE, box=True),
    "single": Preset(font=0.12, margin=0, alignment=5, per_page=1, hi_colour=WHITE),
}


def ass_time(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def style_line(name: str, p: Preset, size: tuple[int, int], colour: str, box: bool) -> str:
    w, h = size
    fs = round(w * p.font)
    if box:
        # BorderStyle 3 draws an opaque box in the outline colour; outline width is its padding
        border, outline, shadow, outline_colour = 3, round(fs * 0.12), 0, PINK
    else:
        border, outline, shadow, outline_colour = 1, round(fs * 0.08), round(fs * 0.05), BLACK
    return (
        f"Style: {name},{FONT},{fs},{colour},{colour},{outline_colour},{SHADOW},-1,0,0,0,"
        f"100,100,0,0,{border},{outline},{shadow},{p.alignment},"
        f"{round(w * 0.08)},{round(w * 0.08)},{round(h * p.margin)},1"
    )


def write_ass(words: list[Word], size: tuple[int, int], style: str, path: Path) -> None:
    p = PRESETS[style]
    w, h = size
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style_line("Word", p, size, WHITE, box=False)}
{style_line("Hi", p, size, p.hi_colour, box=p.box)}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    pages = paginate(words, p.per_page, p.max_chars)
    lines = []
    for n, page in enumerate(pages):
        next_page_start = pages[n + 1][0].start if n + 1 < len(pages) else float("inf")
        texts = [ass_escape(x.text.upper()) for x in page]
        for i, word in enumerate(page):
            # each word stays lit until the next one starts, so the page never blinks
            if i + 1 < len(page):
                end = page[i + 1].start
            else:
                end = min(word.end + TAIL, next_page_start)
            body = " ".join(f"{HIGHLIGHT}{t}{{\\r}}" if j == i else t for j, t in enumerate(texts))
            pop = r"{\fad(80,0)}" if i == 0 else ""
            lines.append(
                f"Dialogue: 0,{ass_time(word.start)},{ass_time(end)},Word,,0,0,0,,{pop}{body}"
            )
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def filter_arg(path: Path) -> str:
    # the filter graph parser needs these characters escaped inside a quoted value
    return "'" + str(path.resolve()).replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:") + "'"


def burn(video: Path, ass: Path, out: Path, extra: list[str] | None = None) -> None:
    vf = f"ass={filter_arg(ass)}"
    if fonts := os.environ.get("SABLYA_FONTS_DIR"):
        vf += f":fontsdir={filter_arg(Path(fonts))}"
    run(tool("ffmpeg"), "-y", "-v", "error", "-i", str(video), "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart", *(extra or []), str(out))


def render(
    video: Path, words: list[Word], size: tuple[int, int], workdir: Path,
    style: str = DEFAULT_STYLE,
) -> Path:
    ass = workdir / "subs.ass"
    out = workdir / f"out-{style}.mp4"
    write_ass(words, size, style, ass)
    burn(video, ass, out)
    return out
