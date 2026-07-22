# デプロイ手順書（Cloud Run + Cloud SQL + GCS + Vercel）

対象: log-analyzer 顧客デモ向けホスティング
作成日: 2026-07-22
方針の根拠: [hosting-refactor-policy.md](./hosting-refactor-policy.md) §6（段階リリース）／
タスク網羅: [deployment-migration-tasks.md](./deployment-migration-tasks.md)

> **本書は「手を動かす順のコマンド集」**。設計判断は上記2書を参照。
> コード改修（PR #15/#16/#17）は main にマージ済み＝**ここからはインフラ構築が中心**。
> 原則: `env 未設定＝ローカル現状維持`。本番化は env を差し替えて段階的に切替える。

---

## 変数（先に決めて全 STEP で使い回す）

```bash
export PROJECT_ID="<BQ と同じ GCP プロジェクト>"
export REGION="asia-northeast1"          # 東京で統一（BQ/GCS/Cloud SQL/Cloud Run）
export REPO="log-analyzer"               # Artifact Registry リポジトリ名
export SERVICE="log-analyzer-api"        # Cloud Run サービス名
export SA="log-analyzer-run"             # 実行用サービスアカウント
export SQL_INSTANCE="log-analyzer-pg"    # Cloud SQL インスタンス
export BUCKET="gs://${PROJECT_ID}-log-analyzer"   # GCS バケット（グローバル一意）
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}"
```

---

## STEP 0. 事前準備（各自の端末）

```bash
# 認証（ユーザー操作。このセッションなら `! gcloud auth login` で実行可）
gcloud auth login
gcloud config set project "$PROJECT_ID"

# 必要 API 有効化
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com storage.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com bigquery.googleapis.com

# Artifact Registry リポジトリ
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION"

# 実行用サービスアカウント（後の ADC 一本化で使う）
gcloud iam service-accounts create "$SA" --display-name="log-analyzer Cloud Run"
export SA_EMAIL="${SA}@${PROJECT_ID}.iam.gserviceaccount.com"
```

---

## STEP 1. まず「今の SQLite/FS のまま」Cloud Run で1回起動

狙い＝**SSE 370s / MCP 常駐 / 実行時間**という“動くか不安な要素”を最初に潰す。
この段階では DB=コンテナ内 SQLite、ストレージ=コンテナ内 FS（揮発）で構わない。

```bash
# ビルド & push（BQ 取得を最初から使うなら INSTALL_TOOLBOX=true）
gcloud builds submit --tag "${IMAGE}:v1" \
  --substitutions=_DUMMY=1 .
# ↑ Dockerfile はリポジトリルート。toolbox 同梱するローカルビルド例:
#   docker build --build-arg INSTALL_TOOLBOX=true --build-arg TOOLBOX_VERSION=0.5.0 \
#     -t "${IMAGE}:v1" . && docker push "${IMAGE}:v1"

# デプロイ（SSE を切らさない3点セット: timeout / min-instances / cpu always-allocated）
gcloud run deploy "$SERVICE" \
  --image="${IMAGE}:v1" \
  --region="$REGION" \
  --service-account="$SA_EMAIL" \
  --timeout=3600 \
  --min-instances=1 \
  --no-cpu-throttling \
  --memory=2Gi --cpu=2 \
  --concurrency=4 \
  --allow-unauthenticated \
  --set-env-vars="ANTHROPIC_API_KEY=<一旦直挿し>,OPENAI_API_KEY=<一旦直挿し>"
```

確認:
- [ ] `GET <URL>/health` が 200
- [ ] 1 解析を投げ **SSE が 370s 級でも切れない**（`--timeout=3600` と `--no-cpu-throttling` が効いているか）
- [ ] メモリは実コードベース＋α（`/tmp`=RAM のため）。OOM が出たら `--memory` を上げる

> ここで通れば「土台OK」。以降は env を差し替えて中身を順に本番化する。

---

## STEP 2. DB を Cloud SQL(Postgres) に向ける

コードは `DATABASE_URL` で両対応済み（PR #16）。接続を差すだけ。

```bash
# 最小構成インスタンス（デモ用途）
gcloud sql instances create "$SQL_INSTANCE" \
  --database-version=POSTGRES_16 --tier=db-f1-micro \
  --region="$REGION" --storage-size=10GB
gcloud sql databases create appdb --instance="$SQL_INSTANCE"
gcloud sql users set-password postgres --instance="$SQL_INSTANCE" --password="<PW>"

# Cloud SQL コネクタで Cloud Run に接続
export CONN_NAME="$(gcloud sql instances describe "$SQL_INSTANCE" --format='value(connectionName)')"
gcloud run services update "$SERVICE" --region="$REGION" \
  --add-cloudsql-instances="$CONN_NAME" \
  --set-env-vars="DATABASE_URL=postgresql://postgres:<PW>@/appdb?host=/cloudsql/${CONN_NAME},DB_POOL_MAX=10"
```

残タスク（コード）:
- [ ] 冪等 DDL / マイグレーション（`ALTER TABLE` 群を Alembic 等へ）
- [ ] プール上限の実測調整（`DB_POOL_MAX`）

---

## STEP 3. ストレージ抽象層 → GCS（最大工数）

コードは `LOG_STORE` / `SOURCE_STORE` で切替済み（PR #17）。バケットを作り env を差す。

```bash
gcloud storage buckets create "$BUCKET" --location="$REGION" \
  --uniform-bucket-level-access
# ライフサイクル（保持/削除）は demo 用途に応じ別途 json で設定

gcloud run services update "$SERVICE" --region="$REGION" \
  --set-env-vars="LOG_STORE=${BUCKET}/logs,SOURCE_STORE=${BUCKET}/source"
```

確認:
- [ ] ログ upload→保存→再読込が GCS 経由で通る
- [ ] コードベース解析（`/tmp` に展開→処理→`.index.json`/`.meta.json` を GCS 書戻し）が動く
- [ ] `/tmp`=RAM のためメモリ再調整（大きめコードベースで OOM なら `--memory` 増）

---

## STEP 4. SA 鍵 → ADC / Secret Manager 化

鍵ファイル配布をやめ、Cloud Run の SA に権限付与（ADC 自動取得）。

```bash
# 実行 SA に BQ/GCS/Cloud SQL 権限
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.jobUser"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.dataViewer"
gcloud storage buckets add-iam-policy-binding "$BUCKET" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectAdmin"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/cloudsql.client"

# 各キーを Secret Manager へ（例: ANTHROPIC_API_KEY）
printf '%s' "<KEY>" | gcloud secrets create ANTHROPIC_API_KEY --data-file=-
gcloud secrets add-iam-policy-binding ANTHROPIC_API_KEY \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor"
# Cloud Run へ注入（直挿し env から切替）
gcloud run services update "$SERVICE" --region="$REGION" \
  --update-secrets="ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest"
# OPENAI_API_KEY / LANGFUSE_* / BIGQUERY_* も同様に
```

- [ ] `GOOGLE_APPLICATION_CREDENTIALS` を **外す**（ADC で自動取得されることを確認）
- [ ] `secrets/sa-key.json` の同梱が無いこと（`.dockerignore` 確認）
- [ ] Langfuse セルフホスト（v2＝Postgres のみ・Cloud SQL 同居可）を別 Cloud Run にデプロイ→`LANGFUSE_HOST` 更新

---

## STEP 5. Vercel 接続 + 公開ゲート（公開前必須）

```bash
# フロント（apps/ui）を Vercel に。Root Directory=apps/ui / build=npm run build / output=dist
#   環境変数: VITE_API_BASE=<Cloud Run URL>  VITE_API_KEY=<API_KEY と一致>
vercel --cwd apps/ui   # または Vercel ダッシュボードで Git 連携

# Cloud Run 側の公開ゲート
gcloud run services update "$SERVICE" --region="$REGION" \
  --update-secrets="API_KEY=API_KEY:latest" \
  --set-env-vars="CORS_EXTRA_ORIGINS=https://<app>.vercel.app,CORS_ALLOW_LOCALHOST=0,RATELIMIT_RUNS_PER_MIN=10"
```

- [ ] SSE がクロスオリジン（Vercel→Cloud Run）で通る
- [ ] `X-API-Key` 未添付リクエストが 401（`/health` は除外）
- [ ] GCP 予算アラート／LLM 側の利用上限を設定

---

## STEP 6. E2E 実測

- [ ] 1 解析を最後まで（**370s SSE 切れない** / 履歴保存 / 評価 / BQ 取得）
- [ ] Langfuse にトレースが飛ぶ
- [ ] 失敗系（保存失敗の可視化）が期待どおり
- [ ] 再デプロイ後もデータが残る（DB=Cloud SQL / ファイル=GCS に外出しできているか＝FS 揮発の影響が無いこと）

---

## 補足・注意

- **課金の伴う作成（Cloud SQL / Cloud Run min-instances=1 / バケット）はユーザー承認前提**。
- toolbox（BQ 取得）を使う場合はイメージビルドで `INSTALL_TOOLBOX=true` を渡すか、
  別 Cloud Run サービスに分離。`config/bigquery_toolbox.yaml` の project/location/allowedDatasets を
  本番の `BIGQUERY_*` と一致させる（env 補間は非対応）。
- `min-instances=1` は常時課金。デモ期間外は `--min-instances=0` に落とすかサービス削除で節約。
- スケール対応（進捗状態・`indexer.py` の `@lru_cache` のインスタンスローカル問題）は
  `min-instances=1` 固定で回避中。多インスタンス化は将来 TODO。

---

## STEP 9. 環境削除（teardown）

デモ終了・作り直し時の後片付け。**課金が続く順（Cloud SQL / Cloud Run min-instances=1）から先に止める**。
STEP 0 で定義した変数がそのまま使える。

### 9-1. まず課金を止める（削除せず一時停止したいだけなら、ここまでで足りる）

```bash
# Cloud Run: 常時課金をゼロに（サービスは残す＝すぐ復帰可能）
gcloud run services update "$SERVICE" --region="$REGION" --min-instances=0
# Cloud SQL: インスタンス停止（データは保持。停止中はストレージ代のみ）
gcloud sql instances patch "$SQL_INSTANCE" --activation-policy=NEVER
```

### 9-2. 完全削除（作り直す／完全撤収する場合）

```bash
# --- Cloud Run サービス ---
gcloud run services delete "$SERVICE" --region="$REGION" --quiet

# --- Cloud SQL（★不可逆。デモデータ残すなら先に export）---
# gcloud sql export sql "$SQL_INSTANCE" "${BUCKET}/backup/appdb-$(date +%F).sql" --database=appdb
gcloud sql instances delete "$SQL_INSTANCE" --quiet

# --- GCS バケット（★中のオブジェクトごと削除）---
gcloud storage rm --recursive "$BUCKET"

# --- Secret Manager（作った分だけ）---
for S in ANTHROPIC_API_KEY OPENAI_API_KEY API_KEY \
         LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY; do
  gcloud secrets delete "$S" --quiet 2>/dev/null || true
done

# --- Artifact Registry（イメージごとリポジトリ削除）---
gcloud artifacts repositories delete "$REPO" --location="$REGION" --quiet

# --- 実行用サービスアカウント（先に IAM バインディングを剥がす）---
for ROLE in roles/bigquery.jobUser roles/bigquery.dataViewer \
            roles/cloudsql.client; do
  gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" --role="$ROLE" --quiet 2>/dev/null || true
done
gcloud iam service-accounts delete "$SA_EMAIL" --quiet

# --- 予算アラート ---
# gcloud billing budgets list / delete で作成した分を削除
```

### 9-3. Vercel 側

```bash
# プロジェクト削除（Git 連携も解除される）
vercel remove <project-name> --yes
# ダッシュボードから削除でも可
```

### 9-4. 後片付けチェック

- [ ] `gcloud run services list --region="$REGION"` に残っていない
- [ ] `gcloud sql instances list` に残っていない
- [ ] `gcloud storage ls` にバケットが無い
- [ ] `gcloud secrets list` に作成した秘密が無い
- [ ] **請求ダッシュボードで翌日に課金がゼロ**（min-instances=1 と Cloud SQL の止め忘れが主因）
- [ ] Langfuse を別サービスで立てた場合、その Cloud Run / DB も同様に削除

> **注意**: `PROJECT_ID` が他用途（BigQuery 本番ルート等）と共用の場合、
> プロジェクトごと削除（`gcloud projects delete`）は**しない**。上記のリソース単位削除に留める。
