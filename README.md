# English Vocab Trainer

This project is being rebuilt as a conventional FastAPI application for Fly.io. The previous Cloudflare prototype is preserved in Git as `cloudflare-prototype-2026-08-09`; it is not the deployment target.

The application is English-only and audio-first. Its SQLite schema is created from versioned SQL migrations, and audio/catalogs/transcripts/secrets are excluded from Git.

## M1/M2b: private catalog, private audio, and publishing

M1 runs locally with SQLite and an audio root. It selects exactly ten files from each approved
source tree (`上級SVL/` and `超上級SVL/`), favouring lower levels and using a deterministic path
tie-break. It never scans the similarly named duplicate directories or CSV files. Audio is served
in place from the configured parent audio root; it is never copied into, or tracked by, this repo.

Create the private catalog (the catalog and all transcripts stay under ignored `.private/`):

`uv run vocab-ingest scan --source .. --catalog .private/m1-catalog.jsonl`

Install the optional Apple Silicon transcription tooling, then generate English-only transcripts:

`uv sync --group ingest`

`uv run --group ingest vocab-ingest transcribe --source .. --catalog .private/m1-catalog.jsonl`

Validate the private 20-record catalog, then dry-run and publish it to a local database. The
filesystem backend is the default and stores the source-relative catalog key in SQLite:

`uv run vocab-ingest validate --source .. --catalog .private/m1-catalog.jsonl`

`uv run vocab-ingest publish --source .. --catalog .private/m1-catalog.jsonl --database ./vocab.db --audio-backend filesystem --dry-run`

`uv run vocab-ingest publish --source .. --catalog .private/m1-catalog.jsonl --database ./vocab.db --audio-backend filesystem`

To publish R2 keys instead, upload the same immutable files first and then select the R2 backend:

`uv run vocab-ingest upload-audio --source .. --catalog .private/m1-catalog.jsonl --dry-run`

`R2_ENDPOINT_URL=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=... uv run vocab-ingest upload-audio --source .. --catalog .private/m1-catalog.jsonl`

`uv run vocab-ingest publish --source .. --catalog .private/m1-catalog.jsonl --database ./vocab.db --audio-backend r2`

`upload-audio` validates all 20 source-relative paths and file SHA-256 values before it contacts
R2. Every object is private at `audio/<64 lowercase sha256>.mp3`, with `Content-Type:
audio/mpeg` and `sha256` metadata. A matching existing object's length and metadata causes a
safe skip, so rerunning resumes automatically. A mismatch fails closed; `--force` is the explicit
overwrite escape hatch. The catalog always retains its local source-relative audio key; it is not
rewritten to an R2 key.

The transcriber defaults to `mlx-community/whisper-large-v3-turbo` with `language=en`. It rejects
Japanese characters and transcripts too short to contain an English definition/example. Use
`--resume` on scan to retain matching existing transcripts, `--force` to transcribe again, and
`--dry-run` on scan/transcribe/publish to verify a planned operation without writes.

Run the application with Python 3.13 and uv. The local filesystem backend requires an explicit
`AUDIO_BACKEND=filesystem`; `AUDIO_ROOT` must be the parent containing the two approved source
directories, so catalog audio keys resolve without copying any MP3:

`VOCAB_DB_PATH=./vocab.db AUDIO_BACKEND=filesystem AUDIO_ROOT=.. APP_ENV=local uv run uvicorn english_vocab_trainer.web.app:app --app-dir src`

The private, server-side R2 proxy uses `AUDIO_BACKEND=r2`, `R2_ENDPOINT_URL`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_BUCKET` (optionally `R2_REGION`, default
`auto`). Objects must use `audio/<64 lowercase hex>.mp3` keys and include lowercase `sha256`
metadata. The browser only receives the authenticated `/api/v1/audio/{word_id}` proxy; neither
R2 URLs nor credentials are exposed. This repository contains no R2 credentials; the upload path
is covered with fakes and dry-runs only.

The MVP proxy buffers one MP3 as bytes for each GET. The measured maximum across the approved
2,000 audio sources is 797,946 bytes (about 0.8 MB), which is acceptable for this bounded slice.
Before serving larger media, replace this with `StreamingResponse` and enforce a size cap.

The web flow is audio first: term and transcript are hidden until **Unknown**, **Known** maps to
Easy for a new word and Good for a reviewed word, and **Unknown** maps to Again. Events are
idempotent UUIDs; Undo either removes an unsynchronised event or calls the void/replay endpoint.
The web client retains its session and outbox in IndexedDB, deleting only server-acknowledged event IDs.

No MP3, catalog, transcript, secret, or database belongs in Git. Production repository/auth
remain fail-closed until M3; M2c is limited to the remaining offline/cache work.

Browser acceptance tests use a live local Uvicorn server and Chromium. Install and run them with:

`uv run playwright install chromium`

`uv run pytest -m e2e --no-cov -W error::ResourceWarning`
