# English Vocab Trainer

An English-only, audio-first PWA for the 2,000 personal SVL recordings. Audio, transcripts, private catalogs, secrets, and source material are deliberately excluded from Git.

## Architecture

`domain` is pure Python (Py-FSRS 6); `application` holds study/review use cases; `ports` define repositories; local and Cloudflare adapters implement them. FastAPI/Jinja2 is the web adapter and `worker.py` is the Cloudflare Python Worker boundary. D1 persists canonical state and idempotent review events; private R2 serves ranged audio with ETags.

## Setup

Install Python 3.13 and uv, then run `uv sync --group dev`, `APP_ENV=local uv run uvicorn english_vocab_trainer.web.app:app --app-dir src`. Use `uv run vocab-ingest validate --source ..` only to audit the two approved source trees. MLX Whisper is an optional Apple Silicon ingest dependency: `uv sync --group ingest`.

## Security and import policy

Production requires a verified `Cf-Access-Jwt-Assertion` (RS256, issuer and audience). Configure `CF_ACCESS_PUBLIC_KEY`, `CF_ACCESS_ISSUER`, and `CF_ACCESS_AUDIENCE` only as Worker secrets. The ingest CLI reads only `上級SVL/` and `超上級SVL/`; it does not read duplicate source folders or CSV metadata. Private output belongs in `.private/`.
