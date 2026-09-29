# Contributing

Issues and pull requests are welcome.

- `uv sync`, then `uv run pytest`. Point `SUBLYA_FFMPEG` at an ffmpeg
  with libass, or the render tests are skipped.
- A change to word timings needs a test on `tests/fixtures/natasha`: it's a real
  recording, and the reference timings there were checked frame by frame.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/), in English.
- Never commit `.env` or tokens. `.env.example` lists every variable with an empty value.
