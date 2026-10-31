# English Vocab Trainer

1人用の英単語学習アプリです。本番は Cloudflare Workers Free 上の Python/FastAPI、D1、非公開 R2、Static Assets で動作します。認証は Cloudflare Access が担当し、Worker 自身も Access JWT、所有者メール、CSRF を検証します。

```text
Browser
  └─ Cloudflare Access
       └─ Python Worker / FastAPI
            ├─ D1: words, reviews, FSRS state
            ├─ private R2: content-addressed MP3
            └─ Static Assets: HTML, JavaScript, manifest
```

Fly.io、Docker、Litestream、アプリ独自パスワード、本番 SQLite は使用しません。旧 SQLite の学習履歴は D1 へ移行せず、D1 は履歴なしで開始します。

## セキュリティ境界

- `assets.run_worker_first=true` により、静的ファイルを含む全リクエストが Worker の認証を通ります。
- Worker は `Cf-Access-Jwt-Assertion` の RS256 署名、issuer、audience、有効期限、所有者メールを検証します。Access 設定が外れても JWT のない要求は 403 です。
- JWKS は Access team domain から取得し、5分だけキャッシュします。未知の `kid` は即時再取得して鍵ローテーションに追従します。
- 書き込みは HTTPS の `Origin`、HttpOnly/Secure/SameSite=Strict の `__Host-vocab-csrf` Cookie、`X-CSRF-Token` を照合します。
- R2 バケットは非公開です。ブラウザは `/api/v1/audio/{word_id}` のみを使い、R2 URLや認証情報を受け取りません。
- API、HTML、音声の Worker 応答には `Cache-Control: no-store` を設定します。
- `/docs`、`/redoc`、`/openapi.json` は本番で公開しません。

Cloudflare の仕様上、Static Assets を組み合わせた Worker では `ctx.access` に制約があるため、JWT を手動検証しています。根拠は [Access JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/) と [Python Workers/FastAPI Static Assets](https://developers.cloudflare.com/workers/languages/python/packages/fastapi/) です。

## 開発と検証

Python 3.13 以上、`uv` 0.13.4 以上、Node.js 22 を使います。Dropbox 配下で仮想環境の読み込みが遅い場合は環境だけを外へ置けます。

```sh
external_env="$(mktemp -d)/venv"
UV_PROJECT_ENVIRONMENT="$external_env" uv sync --group dev --frozen
UV_PROJECT_ENVIRONMENT="$external_env" uv run ruff format --check .
UV_PROJECT_ENVIRONMENT="$external_env" uv run ruff check .
UV_PROJECT_ENVIRONMENT="$external_env" uv run mypy src tests
UV_PROJECT_ENVIRONMENT="$external_env" uv run pytest -m 'not e2e' -W error::ResourceWarning
UV_PROJECT_ENVIRONMENT="$external_env" uv run playwright install chromium
UV_PROJECT_ENVIRONMENT="$external_env" uv run pytest -m e2e --no-cov -W error::ResourceWarning
UV_PROJECT_ENVIRONMENT="$external_env" uv run pywrangler deploy --dry-run --env=""
```

ローカル UI は SQLite と filesystem audio で起動します。

```sh
VOCAB_DB_PATH=./vocab.db \
AUDIO_BACKEND=filesystem \
AUDIO_ROOT=.. \
APP_ENV=local \
uv run uvicorn english_vocab_trainer.web.app:create_app_from_env \
  --factory --app-dir src
```

ローカルアプリは認証なしの開発専用です。本番相当の認証は Worker のテストと Preview 環境で確認します。

## 学習動作

サーバーは期限到来済み、未学習の順に1枚だけ返します。Known は FSRS の Good、Unknown は Again です。新規回答は UUID で冪等化し、同じ UUID と回答の再送は何もしません。同じ UUID の回答を変更すると元の回答日時を保って履歴全体を再計算します。

Previous / Next は50件ずつのサーバー履歴を移動します。複数タブでは Web Locks を取得した1タブだけが回答できます。Service Worker、IndexedDB、offline cache、client-side outbox はありません。

## 非公開カタログ

入力は許可済みの `上級SVL/` と `超上級SVL/` のみです。`full` profile は各1,000件、合計2,000件を要求し、path、term、checksum、英語 transcript、音声サイズを fail closed で検証します。カタログ、文字起こし、SQL、DB、音声を Git に追加してはいけません。

```sh
uv run vocab-ingest scan \
  --profile full --source .. --catalog .private/full-catalog.jsonl --dry-run
scripts/ingest-env scan \
  --profile full --source .. --catalog .private/full-catalog.jsonl
scripts/ingest-env transcribe \
  --profile full --source .. --catalog .private/full-catalog.jsonl
scripts/ingest-env audit \
  --profile full --source .. --catalog .private/full-catalog.jsonl
scripts/ingest-env validate \
  --profile full --source .. --catalog .private/full-catalog.jsonl
```

音声は `audio/<lowercase sha256>.mp3` に変換して R2 へ投入します。1件1 MiB未満、合計8 GB未満でなければ処理を拒否します。アップロード前に dry-run し、既存 object の size と SHA-256 metadata が一致する場合だけ skip します。

```sh
R2_ENDPOINT_URL=... \
R2_ACCESS_KEY_ID=... \
R2_SECRET_ACCESS_KEY=... \
R2_BUCKET=english-vocab-trainer-audio \
scripts/ingest-env upload-audio \
  --profile full --source .. --catalog .private/full-catalog.jsonl --dry-run

# dry-run 成功後、末尾の --dry-run を外す
```

D1 用 SQL は決定的な順序と catalog SHA-256 で生成します。学習履歴は含みません。

```sh
scripts/ingest-env export-d1 \
  --profile full --source .. \
  --catalog .private/full-catalog.jsonl \
  --output .private/full-catalog.sql --dry-run

# dry-run 成功後、末尾の --dry-run を外す
```

## Cloudflare リソースの初期作成

Free plan のアカウントで Wrangler にログインして作成します。有料プラン、Containers、独自ドメイン、R2 Infrequent Access は使用しません。

```sh
npx wrangler login
npx wrangler d1 create english-vocab-trainer
npx wrangler d1 create english-vocab-trainer-preview
npx wrangler d1 create english-vocab-trainer-restore-test
npx wrangler r2 bucket create english-vocab-trainer-audio
npx wrangler r2 bucket create english-vocab-trainer-backup
```

Production と Preview D1 の実 ID を `wrangler.jsonc` の該当する `database_id` に設定します。ゼロ UUID は CI dry-run 用の未設定値であり、そのままデプロイしてはいけません。restore-test D1 は Worker binding に追加しません。

```sh
uv run pywrangler d1 migrations apply DB --remote --env preview
uv run pywrangler d1 migrations apply DB --remote --env=""

uv run pywrangler d1 execute DB --remote --env preview \
  --file .private/full-catalog.sql
uv run pywrangler d1 execute DB --remote --env="" \
  --file .private/full-catalog.sql
```

投入後、両DBで `schema_metadata.catalog_count=2000`、`catalog_sha256` が一致し、`SELECT count(*) FROM words` が2,000であることを確認します。`/readyz` もこの条件を満たさなければ 503 です。

## Cloudflare Access

Production と Preview の `*.workers.dev` URLだけを対象に Access application を作ります。アカウント全体の Worker を一括保護しません。

各 application に次を設定します。

1. 本人の identity/email だけを許可する Allow policy。
2. GitHub Actions readiness 用 service token だけを許可する Service Auth policy。
3. Application Audience (AUD) tag を記録する。
4. Preview URL と Production URL が別 application なら、それぞれの AUD を使う。

Worker には次の値を環境別に登録します。リポジトリへ値をコミットしないでください。

- `ACCESS_TEAM_DOMAIN`: `<team>.cloudflareaccess.com`
- `ACCESS_AUD`: application の AUD tag
- `OWNER_EMAIL`: 許可する本人メール
- `APP_ORIGIN`: `https://<worker>.<subdomain>.workers.dev`
- `HEALTHCHECK_SERVICE_TOKEN_ID`: readiness 用 service token の Client ID（`.access` で終わる値）

```sh
for name in ACCESS_TEAM_DOMAIN ACCESS_AUD OWNER_EMAIL APP_ORIGIN HEALTHCHECK_SERVICE_TOKEN_ID; do
  uv run pywrangler secret put "$name" --env preview
done

for name in ACCESS_TEAM_DOMAIN ACCESS_AUD OWNER_EMAIL APP_ORIGIN HEALTHCHECK_SERVICE_TOKEN_ID; do
  uv run pywrangler secret put "$name" --env=""
done
```

`wrangler.jsonc` の `keep_vars=true` により、デプロイでこれらを削除しません。service token JWT の `common_name` は Client ID であるため、Worker は `/healthz` と `/readyz` だけをこのIDに許可します。

## デプロイ

`.github/workflows/deploy.yml` は手動実行のみです。Preview は任意のブランチ、Production は `main` からだけ実行できます。migration成功後にWorkerをデプロイし、Access service token で `/readyz` を検査します。

GitHub の `preview` と `production` environment に次を設定します。

Secrets:

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_DEPLOY_API_TOKEN`
- `CF_ACCESS_CLIENT_ID`
- `CF_ACCESS_CLIENT_SECRET`

Variable:

- `WORKER_URL`

Deploy token は対象 Worker の編集、対象 D1 の編集、共有 audio R2 binding の参照に必要な最小権限だけを与えます。Production environment には required reviewer を設定します。

## バックアップと復元確認

`.github/workflows/backup.yml` は毎週日曜03:00 JSTに Production D1 を SQL exportし、gzip と SHA-256 manifest を非公開 `english-vocab-trainer-backup` R2 の8固定スロットへ保存します。月初の週と手動指定時は隔離済みの `english-vocab-trainer-restore-test` だけを初期化して復元し、schema version、catalog hash、words、reviews、word_state 件数を照合します。本番DBを直接上書きしません。

Production environment に `CLOUDFLARE_BACKUP_API_TOKEN` を追加します。この token は Production D1 export、restore-test D1 write、backup R2 read/writeだけを許可します。

障害復旧は最新 manifest とSQLを新しいD1へ復元し、照合後に Worker binding を新DBへ切り替えます。D1 Time Travel も利用できますが、週次R2コピーとは独立に扱います。

## CI と完了条件

CI は以下をすべて検査します。

- Ruff format/lint、strict mypy
- 非E2Eテストと branch coverage 90%以上
- Chromium E2E
- wheel build と隔離環境での import smoke
- Python Worker bundle の dry-run
- private catalog、音声、DB がGit管理されていないこと

本番移行完了は次をすべて満たした状態です。

- Production/Preview が Access で保護され、本人以外は拒否される
- Access を迂回しても Worker が fail closed で403を返す
- D1に監査済み2,000語があり `/readyz` が200を返す
- Known / Unknown / Previous / Next、50件超の履歴、回答修正、UUID再送が正常
- PCとスマートフォンで同じ履歴が見える
- 音声 GET/HEAD、Range、シーク、ETag が正常
- R2に公開URLがなく、private responseがキャッシュされない
- バックアップを隔離D1へ復元できる
- CIが成功し、Cloudflareの予測請求額が0ドル
- Fly.io、Docker、Litestreamの本番コードが残っていない

## ロールバック

Cloudflare移行前のFly環境は、このリポジトリから自動削除しません。新環境の受け入れが完了するまで外部リソースを保持します。コードのロールバックは旧デプロイへ切り戻し、D1/R2は削除せず証跡として残します。新D1の学習履歴を旧SQLiteへ逆移行する機能はありません。
