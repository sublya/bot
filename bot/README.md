# sublya bot

The Telegram bot and the subtitle engine behind it. Python 3.12, [uv](https://docs.astral.sh/uv/),
aiogram 3, SQLite, ffmpeg with libass.

```
sublya/
  core/     # no Telegram here: audio, speech recognition, word alignment, rendering, CLI
  bot/      # aiogram handlers, the SQLite job queue, the worker
tests/
  fixtures/natasha/   # recognised words, pauses and the reference poem of a real recording
```

## How a video becomes subtitles

1. ffmpeg extracts 16 kHz mono audio and finds pauses with `silencedetect`.
2. An OpenAI-compatible `audio/transcriptions` endpoint (OpenRouter by default) returns words
   with start and end times.
3. Word starts are snapped to the pauses: recognisers are off by a couple of hundred
   milliseconds, while the end of a pause is exactly where the next word begins. See
   [docs/alignment.md](../docs/alignment.md).
4. If the user sent a reference text, its wording replaces what was heard and keeps the timings.
5. Words are grouped into screens of up to 3 words and 24 characters, one ASS event per word,
   and ffmpeg burns them into the video.

The transcript is cached per job, so the "fix text" and "style" buttons re-render without
calling the recogniser again.

## Configuration

Everything comes from the environment; `.env.example` lists it.

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | required | token from @BotFather |
| `STT_API_KEY` | required | key for the transcription endpoint |
| `STT_API_URL` | `https://openrouter.ai/api/v1` | any OpenAI-compatible base URL, e.g. Groq |
| `STT_MODEL` | `openai/whisper-large-v3-turbo` | a Whisper model that returns word timestamps |
| `STT_LAG` | tuned per timing kind | how late the recogniser places word starts, seconds |
| `STT_DAILY_MINUTES` | `300` | recognised minutes per day for the whole bot |
| `DAILY_VIDEOS` | `10` | new videos per user per day; re-renders are free |
| `ADMIN_IDS` | empty | Telegram user ids that get errors and `/stats` |
| `TELEGRAM_API_URL` | empty | local Bot API server; without it files over 20 MB can't be downloaded |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | | from my.telegram.org, for the local Bot API server |
| `DATA_DIR` | `data` | SQLite database and job files, removed after 24 hours |
| `SUBLYA_FFMPEG`, `SUBLYA_FFPROBE` | from `PATH` | ffmpeg must have libass |
| `SUBLYA_FONTS_DIR` | | where libass looks for Montserrat ExtraBold |

## Run with Docker

```bash
cp .env.example .env
docker compose up -d --build
```

This starts the bot next to a local Bot API server. A bot that has been polling the public
Bot API has to be logged out of it once, otherwise the local server can't take it over:

```bash
curl https://api.telegram.org/bot$BOT_TOKEN/logOut
```

## Develop

```bash
uv sync
uv run pytest
```

Render tests need ffmpeg with libass and are skipped otherwise. Homebrew's `ffmpeg` has no
libass, `ffmpeg-full` does:

```bash
export SUBLYA_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg SUBLYA_FFPROBE=/opt/homebrew/opt/ffmpeg-full/bin/ffprobe
```

Run the bot without Docker, polling the public Bot API:

```bash
uv run --env-file .env sublya-bot
```

## CLI

The engine works without Telegram:

```bash
uv run --env-file .env python -m sublya.core video.mov --text poem.txt --style big
```

The transcript is cached in `work/<video>/transcript.json` next to the video, so trying other
texts and styles is free. `--retranscribe` asks the recogniser again.

Styles: `classic` (white, the current word yellow), `big` (larger, higher), `box` (the current
word on a pink plate), `single` (one word at a time in the centre).
