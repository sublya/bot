# sablya

Telegram bot that burns TikTok-style subtitles with the spoken word highlighted.

Send it a video and it replies with the same video, two or three words on screen at a time.
A caption with the exact text (a poem, a script) replaces what was heard: the wording comes
from the caption, the timings from the speech. The reply has buttons to fix the text or
switch the style without recognising the audio again.

## Run it

```bash
cp .env.example .env
```

Fill in `BOT_TOKEN`, `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` from my.telegram.org and
`STT_API_KEY`. The bot talks to a local Bot API server, since the public one won't hand out
files over 20 MB. A bot that has been polling the public API has to be logged out of it once:

```bash
curl https://api.telegram.org/bot$BOT_TOKEN/logOut
```

```bash
docker compose up -d --build
```

## CLI

The core works without Telegram. It needs ffmpeg with libass; on macOS that's `ffmpeg-full`.

```bash
SABLYA_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg SABLYA_FFPROBE=/opt/homebrew/opt/ffmpeg-full/bin/ffprobe uv run --env-file .env python -m sablya.core video.mov --text poem.txt --style big
```

## Tests

```bash
uv run pytest
```

Render tests are skipped unless `SABLYA_FFMPEG` points to an ffmpeg with libass.
