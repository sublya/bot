# Sublya

A Telegram bot that burns TikTok-style subtitles into a video: two or three words on screen,
the word being spoken lit up. Send it a video, get the same video back with subtitles.

Try it: [@sublyarobot](https://t.me/sublyarobot) · site: [sublya.aimuzov.online](https://sublya.aimuzov.online)

- **Timing that holds.** Recognised word times are snapped to the pauses in the audio, so
  words light up when they're said, not a quarter of a second off.
  How: [docs/alignment.md](docs/alignment.md).
- **Your exact text.** Put a poem or a script in the video caption: the screen shows its words,
  the timings come from the speech. Mumbled, skipped or extra words don't break the sync.
- **Fix without re-recognising.** Buttons under the result let you correct the text or pick
  another style; the video is re-rendered from the cached transcript.
- **Four styles.** Classic yellow highlight, bigger text, a coloured plate under the current
  word, or one word at a time.
- **Cheap to run.** Recognition goes to Whisper through OpenRouter (about $0.0002 per minute),
  rendering is ffmpeg on the CPU. A 1 vCPU box is enough.

## Repository

| Path | What |
|---|---|
| [`bot/`](bot) | the bot and the subtitle engine, Python; its README covers setup and configuration |
| [`site/`](site) | the landing page, static HTML |
| [`deploy/`](deploy) | production compose file and deploy script |
| [`docs/`](docs) | how word timings work, how deployment works |

## Quick start

```bash
cd bot
cp .env.example .env   # BOT_TOKEN, STT_API_KEY, TELEGRAM_API_ID, TELEGRAM_API_HASH
docker compose up -d --build
```

Details in [bot/README.md](bot/README.md), production setup in [docs/deploy.md](docs/deploy.md).

## License

[MIT](LICENSE)
