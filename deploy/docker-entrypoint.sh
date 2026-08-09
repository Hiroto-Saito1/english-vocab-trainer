#!/bin/sh
set -eu

for required in \
  APP_ENV VOCAB_DB_PATH APP_PASSWORD_HASH SESSION_SIGNING_SECRET \
  AUDIO_BACKEND R2_ENDPOINT_URL R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET \
  LITESTREAM_ACCESS_KEY_ID LITESTREAM_SECRET_ACCESS_KEY \
  LITESTREAM_R2_ENDPOINT_URL LITESTREAM_R2_BUCKET
do
  if ! printenv "$required" >/dev/null 2>&1 || [ -z "$(printenv "$required")" ]; then
    echo "required environment variable is missing: $required" >&2
    exit 64
  fi
done

if { ! printenv ALLOWED_HOSTS >/dev/null 2>&1 || [ -z "$(printenv ALLOWED_HOSTS)" ]; } \
  && { ! printenv FLY_APP_NAME >/dev/null 2>&1 || [ -z "$(printenv FLY_APP_NAME)" ]; }; then
  echo "ALLOWED_HOSTS or FLY_APP_NAME is required" >&2
  exit 64
fi

case "$VOCAB_DB_PATH" in
  /data/*) ;;
  *)
    echo "VOCAB_DB_PATH must be an absolute path below /data" >&2
    exit 64
    ;;
esac
case "$VOCAB_DB_PATH" in
  */../*|*/..|*/./*|*/.|*//*|*/|*[!\ -~]*)
    echo "VOCAB_DB_PATH must be a canonical database file path below /data" >&2
    exit 64
    ;;
esac
if [ -n "$(printf %s "$VOCAB_DB_PATH" | tr -d 'A-Za-z0-9._/-')" ]; then
  echo "VOCAB_DB_PATH contains unsupported characters" >&2
  exit 64
fi

bucket_length=${#LITESTREAM_R2_BUCKET}
case "$LITESTREAM_R2_BUCKET" in
  [a-z0-9]* ) ;;
  *)
    echo "LITESTREAM_R2_BUCKET must be an R2-safe bucket name" >&2
    exit 64
    ;;
esac
case "$LITESTREAM_R2_BUCKET" in
  *[!a-z0-9.-]*|.*|*.|*..*)
    echo "LITESTREAM_R2_BUCKET must be an R2-safe bucket name" >&2
    exit 64
    ;;
esac
if [ "$bucket_length" -lt 3 ] || [ "$bucket_length" -gt 63 ]; then
  echo "LITESTREAM_R2_BUCKET must be an R2-safe bucket name" >&2
  exit 64
fi

case "$LITESTREAM_R2_ENDPOINT_URL" in
  https://*.r2.cloudflarestorage.com) endpoint_host=${LITESTREAM_R2_ENDPOINT_URL#https://} ;;
  *)
    echo "LITESTREAM_R2_ENDPOINT_URL must be a Cloudflare R2 HTTPS endpoint" >&2
    exit 64
    ;;
esac
account_id=${endpoint_host%.r2.cloudflarestorage.com}
if [ -z "$account_id" ] || [ -n "$(printf %s "$account_id" | tr -d 'A-Za-z0-9')" ]; then
  echo "LITESTREAM_R2_ENDPOINT_URL must be a Cloudflare R2 HTTPS endpoint" >&2
  exit 64
fi

if [ "$APP_ENV" != "production" ] || [ "$AUDIO_BACKEND" != "r2" ]; then
  echo "production entrypoint requires APP_ENV=production and AUDIO_BACKEND=r2" >&2
  exit 64
fi

mkdir -p "${VOCAB_DB_PATH%/*}"
# /data is the sole Fly volume. A seeded/restored DB can be root-owned, so
# ownership is deliberately limited to this dedicated mount before privilege drop.
chown -R vocab:vocab /data
exec setpriv --reuid=vocab --regid=vocab --init-groups litestream replicate \
  -config /etc/litestream.yml -restore-if-db-not-exists \
  -exec "/app/.venv/bin/uvicorn english_vocab_trainer.web.app:create_app_from_env --factory --host 0.0.0.0 --port 8080 --workers 1 --proxy-headers --forwarded-allow-ips='*'"
