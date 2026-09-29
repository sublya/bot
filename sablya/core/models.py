import json
from dataclasses import asdict, dataclass

STYLES = ("classic", "big", "box", "single")
DEFAULT_STYLE = "classic"


@dataclass
class Word:
    text: str
    start: float
    end: float
    line_end: bool = False


@dataclass
class Transcript:
    """What recognition produced for a video. Cached per job, so re-renders skip the STT call."""

    words: list[Word]
    silences: list[tuple[float, float]]
    size: tuple[int, int]
    duration: float
    lang: str | None = None

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, data: str) -> "Transcript":
        d = json.loads(data)
        return cls(
            words=[Word(**w) for w in d["words"]],
            silences=[tuple(p) for p in d["silences"]],
            size=tuple(d["size"]),
            duration=d["duration"],
            lang=d.get("lang"),
        )
