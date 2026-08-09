# English Vocab Trainer

This project is being rebuilt as a conventional FastAPI application for Fly.io. The previous Cloudflare prototype is preserved in Git as `cloudflare-prototype-2026-08-09`; it is not the deployment target.

The application is English-only and audio-first. Its SQLite schema is created from versioned SQL migrations, and audio/catalogs/transcripts/secrets are excluded from Git.

Run locally with Python 3.13 and uv:

`VOCAB_DB_PATH=./vocab.db AUDIO_ROOT=./audio APP_ENV=local uv run uvicorn english_vocab_trainer.web.app:app --app-dir src`

Create a private audio directory separately; no MP3 belongs in this repository. Before a Fly deployment, set the database path and audio volume mount, apply migrations by starting the application, and configure the authentication layer (M1). Production without configured authentication fails closed.
