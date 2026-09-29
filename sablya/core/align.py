"""Word timings: snapping to pauses, swapping in a reference text, splitting into screens."""

import difflib
import re
from dataclasses import replace

from .models import Transcript, Word

PAGE_GAP = 0.6  # a pause longer than this starts a new page
PER_PAGE = 3
MAX_CHARS = 24


def snap(
    words: list[Word], pauses: list[tuple[float, float]], duration: float, lag: float = 0.0
) -> list[Word]:
    """Recognisers mark a word a bit after it starts sounding. A pause that ends between two
    words' marks is exactly where the later word begins."""
    out = [replace(w) for w in words]
    prev = 0.0
    for w in out:
        resumed = [e for s, e in pauses if prev < e <= w.start]
        prev = w.start
        w.start = resumed[-1] if resumed else max(w.start - lag, 0)
    for i, w in enumerate(out):
        nxt = out[i + 1].start if i + 1 < len(out) else min(w.start + 2, duration)
        paused = [s for s, e in pauses if w.start < s < nxt]
        w.end = paused[0] if paused else nxt
    return out


def normalize(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower().replace("ё", "е"))


def read_text(text: str) -> list[Word]:
    words: list[Word] = []
    for line in text.splitlines():
        for tok in line.split():
            if words and not normalize(tok):
                words[-1].text += " " + tok  # a lone dash belongs to the previous word
            else:
                words.append(Word(tok, 0, 0))
        if words:
            words[-1].line_end = True
    return words


def apply_text(heard: list[Word], ref: list[Word]) -> list[Word]:
    """Keeps the timings of what was heard and the wording of the reference. Unmatched runs
    share their time span in proportion to word length."""
    sm = difflib.SequenceMatcher(
        a=[normalize(w.text) for w in heard], b=[normalize(w.text) for w in ref], autojunk=False
    )
    out: list[Word] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "delete":
            continue
        if op == "equal":
            for h, r in zip(heard[i1:i2], ref[j1:j2]):
                out.append(Word(r.text, h.start, h.end, r.line_end))
            continue
        start = heard[i1].start if i1 < i2 else (out[-1].end if out else 0)
        end = heard[i2 - 1].end if i1 < i2 else (heard[i2].start if i2 < len(heard) else start)
        if end - start < 0.1 * (j2 - j1) and out:
            # nothing heard in between: borrow the tail of the previous word
            start = out[-1].start + (out[-1].end - out[-1].start) / 2
            out[-1].end = start
        total = sum(len(r.text) for r in ref[j1:j2])
        t = start
        for r in ref[j1:j2]:
            dur = (end - start) * len(r.text) / total
            out.append(Word(r.text, t, t + dur, r.line_end))
            t += dur
    return out


def paginate(
    words: list[Word], per_page: int = PER_PAGE, max_chars: int = MAX_CHARS
) -> list[list[Word]]:
    pages: list[list[Word]] = []
    page: list[Word] = []
    for w in words:
        if page and (
            len(page) >= per_page
            or page[-1].line_end
            or w.start - page[-1].end > PAGE_GAP
            or page[-1].text[-1] in ".?!…"
            or sum(len(x.text) + 1 for x in page) + len(w.text) > max_chars
        ):
            pages.append(page)
            page = []
        page.append(w)
    if page:
        pages.append(page)
    return pages


def align(transcript: Transcript, text: str | None = None, lag: float = 0.0) -> list[Word]:
    words = snap(transcript.words, transcript.silences, transcript.duration, lag)
    ref = read_text(text) if text else []
    return apply_text(words, ref) if ref else words
