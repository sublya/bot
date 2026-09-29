import json
import re
from pathlib import Path

from .ffmpeg import run, tool

SILENCE = "silencedetect=n=-35dB:d=0.25"


def video_size(video: Path) -> tuple[int, int]:
    info = json.loads(run(
        tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:stream_side_data=rotation",
        "-of", "json", str(video),
    ).stdout)["streams"][0]
    w, h = info["width"], info["height"]
    rotation = next((d.get("rotation", 0) for d in info.get("side_data_list", [])), 0)
    # ffmpeg autorotates on decode, so the frame the subtitles land on is swapped
    return (h, w) if abs(rotation) % 180 == 90 else (w, h)


def duration(media: Path) -> float:
    return float(run(tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(media)).stdout)


def extract_audio(video: Path, audio: Path) -> None:
    run(tool("ffmpeg"), "-y", "-v", "error", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio))


def silences(audio: Path) -> list[tuple[float, float]]:
    log = run(tool("ffmpeg"), "-v", "info", "-i", str(audio), "-af", SILENCE,
              "-f", "null", "-").stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    return list(zip(starts, ends))
