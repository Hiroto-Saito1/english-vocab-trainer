# English Vocab Trainer

English Vocab Trainer は、英語音声を起点にして単語を学習する、1 アカウント用の FastAPI/PWA アプリです。Fly.io 上の 1 Machine で動かし、学習履歴は SQLite、非公開音声は Cloudflare R2、バックアップは Litestream で管理します。以前の Cloudflare 版は Git の `cloudflare-prototype-2026-08-09` に保存していますが、現在のデプロイ対象ではありません。

音声、カタログ、文字起こし、認証情報、生成した SQLite データベースは Git に含めません。現行スキーマのみをサポートし、旧 DB の upgrade は行いません。旧版から移行する場合は DB を再作成し、カタログを再 publish してください。その際、学習履歴は破棄されます。

## 開発と検証

Python 3.13 と `uv` を使います。

```sh
uv sync --group dev --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest -m 'not e2e' -W error::ResourceWarning
uv run coverage report --precision=2 --fail-under=90
```

PWA のブラウザテストには Chromium が必要です。

```sh
uv run playwright install chromium
uv run pytest -m e2e --no-cov -W error::ResourceWarning
```

E2E はローカル Uvicorn と実際の Chromium を使い、Service Worker、Cache Storage、IndexedDB の状態を固定 sleep に頼らず待ち合わせます。Dropbox 配下の `.venv` で読み込みが滞る環境では、依存環境だけを Dropbox 外に作成できます。

```sh
external_env="$(mktemp -d)/venv"
UV_PROJECT_ENVIRONMENT="$external_env" uv sync --group dev --frozen
UV_PROJECT_ENVIRONMENT="$external_env" uv run pytest
```

## 非公開カタログと文字起こし

ingest は許可した `上級SVL/` と `超上級SVL/` だけを読みます。似た名前のディレクトリや CSV は読みません。`mvp` プロファイルは各ソースから低いレベルを優先し、パスを決定的なタイブレークにして既定 10 件ずつを選びます。`full` は各ソースに解析可能な MP3 が正確に 1,000 件ある場合だけ成功し、2,000 件全ての checksum と path/checksum/term の重複を検証します。

出力先には Git 管理外の `.private/` を使ってください。MVP カタログの基本手順は次の通りです。

```sh
uv run vocab-ingest scan --source .. --catalog .private/mvp-catalog.jsonl
uv sync --group ingest
uv run --group ingest vocab-ingest transcribe --source .. --catalog .private/mvp-catalog.jsonl
uv run vocab-ingest validate --source .. --catalog .private/mvp-catalog.jsonl
uv run vocab-ingest publish --source .. --catalog .private/mvp-catalog.jsonl --database ./vocab.db --audio-backend filesystem --dry-run
uv run vocab-ingest publish --source .. --catalog .private/mvp-catalog.jsonl --database ./vocab.db --audio-backend filesystem
```

`scan` は音声の checksum と provenance manifest を毎回検証し、同じ checksum の検証済み文字起こしを既定で再利用します。catalog、manifest、文字起こし journal、未解決エラーの整合性が取れない場合は fail closed にします。全てを意図的に破棄する場合だけ `scan --force` を使います。`--dry-run` は書き込みなしで予定処理を確認します。

文字起こしの既定モデルは `mlx-community/whisper-large-v3-turbo`、言語は `en` です。`temperature=0` かつ previous-text conditioning なしの決定的なデコードで、短い音声の反復 hallucination を抑えます。日本語、短すぎる文、不安全な Unicode、過度な反復などは拒否します。成功した文字起こしは append-only journal へ fsync し、全件完了時に JSONL catalog へ atomic にまとめます。クラッシュ時に壊れ得るのは journal 最後の不完全な 1 行だけで、再実行で自動継続します。

`--continue-on-error` では、失敗した checksum ごとの未解決状態を atomic な `.transcription-errors.jsonl` snapshot に保存します。理由は `runtime-failure` や品質・script 検証コードなどの allowlist に限り、例外文、path、secret、文字起こし本文は書きません。成功済み checksum は再開時に対象から外れ、未解決エラーがなくなれば snapshot も削除されます。

本番用の full catalog は Dropbox 内の仮想環境を使わず、次の wrapper から実行します。

```sh
uv run vocab-ingest scan --profile full --source .. --catalog .private/full-catalog.jsonl --dry-run
scripts/ingest-env doctor
scripts/ingest-env scan --profile full --source .. --catalog .private/full-catalog.jsonl
scripts/ingest-env status --profile full --source .. --catalog .private/full-catalog.jsonl
scripts/ingest-env transcribe --profile full --source .. --catalog .private/full-catalog.jsonl --max-records 100
scripts/ingest-env audit --profile full --source .. --catalog .private/full-catalog.jsonl
scripts/ingest-env validate --profile full --source .. --catalog .private/full-catalog.jsonl
```

最初は小さな batch で private status/error report を確認し、問題がなければ繰り返すか上限を外します。現在の 2,000 ファイルは合計 7.783 時間、最長 35.344 秒です。ウォームアップ済みの小規模検証からは全件 18〜25 分が目安ですが、性能保証ではありません。

`audit` は inventory、checksum、一意性、source の存在、英語 transcript をブロッキング検証します。term 不在、重複 transcript、不自然な長さは機械可読な private warning report に出します。指摘を手動で確認・修正し、再度 scan/audit してから publish してください。importer は音声ツリーを読むだけで、そこに書き込みません。

## 音声の保管と配信

ローカルでは `filesystem` backend を明示し、`AUDIO_ROOT` に 2 つの許可済みディレクトリの親を指定します。MP3 を repository にコピーする必要はありません。

```sh
VOCAB_DB_PATH=./vocab.db AUDIO_BACKEND=filesystem AUDIO_ROOT=.. APP_ENV=local uv run uvicorn english_vocab_trainer.web.app:create_app_from_env --factory --app-dir src
```

R2 へ publish する場合は、同じ immutable 音声を先に upload します。

```sh
uv run vocab-ingest upload-audio --source .. --catalog .private/mvp-catalog.jsonl --dry-run
R2_ENDPOINT_URL=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=... uv run vocab-ingest upload-audio --source .. --catalog .private/mvp-catalog.jsonl
uv run vocab-ingest publish --source .. --catalog .private/mvp-catalog.jsonl --database ./vocab.db --audio-backend r2
scripts/ingest-env upload-audio --profile full --source .. --catalog .private/full-catalog.jsonl --dry-run
scripts/ingest-env publish --profile full --source .. --catalog .private/full-catalog.jsonl --database ./vocab.db --audio-backend r2 --dry-run
```

`upload-audio` は R2 に接続する前に全 path と SHA-256 を検証します。object key は `audio/<64 文字の lowercase sha256>.mp3`、Content-Type は `audio/mpeg`、metadata の `sha256` も lowercase です。既存 object の size と metadata が一致すれば安全に skip し、不一致なら fail closed にします。上書きは `--force` の場合だけです。catalog 内の audio key は source 相対 path のままで、R2 key へ書き換えません。

ブラウザは認証済みの `/api/v1/audio/{word_id}` proxy だけにアクセスし、R2 URL や credential を受け取りません。現在の proxy は GET ごとに MP3 1 件を bytes として buffer します。許可済み 2,000 件の実測最大値は 797,946 bytes（約 0.8 MB）です。より大きな media を扱う前に `StreamingResponse` と size 上限を導入してください。

## 学習とオフライン動作

学習画面は audio-first です。回答前は term と transcript を隠します。**Known** は初回なら FSRS の Easy、review 済みなら Good、**Unknown** は Again として保存します。review event は UUID で冪等であり、同じ UUID で異なる内容を送れば conflict になります。Undo は未 sync event を端末から削除し、sync 済みなら server で void して、void されていない event から FSRS の due を決定的に再生します。

server は日次の選出結果を session として永続化しません。学習対象と outbox はブラウザの IndexedDB に保存し、server から `applied`、`idempotent`、`voided` が返った event ID だけを削除します。保存する表示状態は操作でのみ更新し、待機中と完了は cards/current/learning queue から描画時に導出します。

オンラインで学習対象を取得すると、PWA は全ての full `200` audio response を private Cache Storage へ preload します。失敗しても online 再生は継続できます。新しい学習対象では古い audio cache を先に削除し、中断した保存済み学習を開き直す場合は対応する audio を保持します。preload の世代は process-local な epoch だけで管理し、古い世代の応答が現在の cache に混入するのを防ぎます。

offline reload でも保存した学習を再開できます。通常 GET は cache の full response、single byte range は正しい `206` として合成します。Service Worker は API、review、認証 JSON や upstream の `206` を cache しません。static shell は network-first、失敗時だけ cache fallback です。初回から offline の場合や、完了後に新しい学習を取得できない場合は、既存の local data を壊さず再接続を促します。

`window.clearPrivateCaches()` は明示的な端末内 privacy boundary です。audio cache を削除し、IndexedDB の学習状態と outbox を transaction で空にします。DB 自体を delete しないのは、生きている browser connection による永続的な block を避けるためです。audio cache の rotate だけでは IndexedDB の学習状態や outbox を削除しません。

## 認証とセキュリティ

本番起動には `APP_ENV=production`、`VOCAB_DB_PATH`、完全な private audio backend 設定、`APP_PASSWORD_HASH`、`SESSION_SIGNING_SECRET` が必要です。`APP_PASSWORD_HASH` は少なくとも memory 19 MiB、2 iterations、16-byte salt/hash の Argon2id encoded hash、`SESSION_SIGNING_SECRET` は 32 random bytes 以上に decode できる canonical base64url 値でなければなりません。shell history に値を残さず生成し、secret manager に登録してください。

```sh
uv run python -c 'from argon2 import PasswordHasher; import getpass; print(PasswordHasher().hash(getpass.getpass("Password: ")))'
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

本番 session cookie は `__Host-`、Secure、HttpOnly、SameSite=Strict で、有効期間は 30 日です。別の CSRF cookie は PWA が一致する header を送るためだけに non-HttpOnly とします。どちらも IndexedDB や Cache Storage に保存しません。login/auth と audio 以外の API response は `no-store` です。private full audio だけを PWA の明示的な現行 cache に保存できます。

パスワード検証は永続化した global limiter で、15 分の rolling window 内に最大 5 回までです。login 成功時に window をクリアします。spoof できる client IP header を信頼せず、local DoS の可能性を受け入れる 1 アカウント用のトレードオフです。Logout は先に端末内学習 data を消去し、その後 server cookie を失効させます。local 消去後に network が切れた場合は、cookie logout を完了するための再接続を促します。

本番では Host header を `ALLOWED_HOSTS` または安全に導出した `FLY_APP_NAME.fly.dev` と照合し、HSTS、CSP、`nosniff`、referrer policy、permissions policy を付与します。Uvicorn は Fly の trusted proxy header を有効にし、HTTPS same-origin 検証が元の scheme を参照できるようにします。

## Fly.io での運用

本番構成は 1 Fly Machine、1 つの `/data` volume、Litestream 0.5.16 です。repository は Fly application、volume、R2 bucket、credential を作成しません。container は Uvicorn worker 1 つで動き、SQLite は全 connection で foreign keys、WAL、5 秒の busy timeout、`synchronous=NORMAL` を使います。volume を root で準備した後、Litestream と application は `vocab` user で動きます。

`fly.toml` は deploy 可能な実 app 設定ではなく template です。そのまま `fly deploy` に渡さないでください。app 名と volume 名を明示し、検証済みの一時 config を作ります。

```sh
FLY_APP=your-app FLY_VOLUME=your_single_volume python deploy/render_fly_config.py --output /tmp/fly.toml --app "$FLY_APP" --volume "$FLY_VOLUME"
FLY_APP=your-app FLY_VOLUME=your-volume fly apps create "$FLY_APP"
fly volumes create "$FLY_VOLUME" --app "$FLY_APP" --region nrt --size 1
fly scale count 1 --app "$FLY_APP"
fly deploy --remote-only --app "$FLY_APP" --config /tmp/fly.toml
```

production entrypoint が受け付けるのは `APP_ENV=production`、`AUDIO_BACKEND=r2`、`/data` 配下の canonical absolute `VOCAB_DB_PATH`（通常 `/data/vocab.db`）です。必要な auth/audio/backup 設定が不足していれば secret を出力せず起動に失敗します。Fly の SIGTERM 後の kill timeout は 45 秒、Litestream の graceful shutdown 時の最終 sync 予算は 30 秒です。`/healthz` は公開 liveness、`/readyz` は公開 readiness で、SQLite の現行スキーマを開いて軽く検査しますが data は返しません。

本番で必要な環境変数は次の通りです。

- `APP_ENV=production`
- `VOCAB_DB_PATH=/data/vocab.db`
- `APP_PASSWORD_HASH`
- `SESSION_SIGNING_SECRET`
- `AUDIO_BACKEND=r2`
- `R2_ENDPOINT_URL`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET`
- `LITESTREAM_ACCESS_KEY_ID`
- `LITESTREAM_SECRET_ACCESS_KEY`
- `LITESTREAM_R2_ENDPOINT_URL`
- `LITESTREAM_R2_BUCKET`

Fly では `FLY_APP_NAME` が platform から渡されます。追加 host が必要なときだけ `ALLOWED_HOSTS` を設定します。backup endpoint は厳密な `https://ACCOUNTID.r2.cloudflarestorage.com` 形式、bucket は lowercase の R2-safe 名に限ります。これは環境変数展開が Litestream YAML の構造を変えるのを防ぐためです。secret は shell history ではなく、例えば Git 管理外の file から登録します。

```sh
fly secrets import --app "$FLY_APP" < .private/fly-production.env
```

audio read、offline upload/publish、Litestream backup には別々の最小権限 R2 credential を使います。app は private audio bucket の read/HEAD だけ、upload/publish は audio prefix の write（必要時だけ overwrite）、Litestream は backup bucket/path の read/list/write だけを持ちます。Litestream は DeleteObject 権限を持たず、`retention.enabled` も false です。固定 path `backups/sqlite/vocab.db`、1 replica、10 秒間隔の sync、6 時間ごとの validation、compaction verification を使います。この設定は LTX chain の誤削除を避ける代わりに immutable growth を生むため、object lifecycle を変更するときは chain 全体から restore できる policy を事前検証してください。

初回 deploy 前に、full catalog の audit/validate、2,000 音声の upload と metadata/size 検証、履歴のない R2-key SQLite seed の publish、Fly volume への seed または Litestream replica の初期化と検証をこの順に完了させます。acceptance 用の学習履歴入り DB は seed に使わないでください。`restore-if-db-not-exists` は検証済み replica があれば restore しますが、seed/replica がなければ空の DB を初期化できてしまいます。その初回起動は成功した catalog deploy とみなさず block として扱います。

## バックアップとリストア

restore drill は live DB を残したまま、必ず新しい path に実行します。

```sh
vocab-restore-drill --live-db /data/vocab.db --output /data/drills/restore-$(date +%Y%m%dT%H%M%S).db
```

live DB とその WAL/SHM、および既存 output は拒否します。Litestream で新規 file へ restore した後、`PRAGMA quick_check`、現行の正確な table/column 構成を検査し、word/review 件数だけを出力します。本番で使う前に backup R2 credential で手動リハーサルを行ってください。実際の障害復旧では app を止め、復旧済み volume/DB を attach または交換します。live DB をその場で上書きする機能はありません。

restore 時間は backup size と R2 の可用性に依存します。1 Machine の個人利用構成では downtime と RTO のリスクを受け入れています。render された Fly mount は副次的な便利機能として 14 snapshots を保持しますが、DR の主要 copy は R2 replication です。

GitHub の手動 **Deploy** workflow は `workflow_dispatch` のみで、保護された `production` environment から `FLY_API_TOKEN`、`FLY_APP`、`FLY_VOLUME` を実行時に受け取ります。CI は deploy せず、container build、固定 Litestream binary、wheel と container package からの現行スキーマ DB 作成を検査します。
