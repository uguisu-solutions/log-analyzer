# クラウド移行タスク一覧

## 目標アーキテクチャ

| レイヤ | 採用先 | 備考 |
|---|---|---|
| フロントエンド | Vercel（独自ドメインなし＝`*.vercel.app`） | Vite/React。秘密情報は置かない |
| バックエンド | Google Cloud Run（推奨）or AWS App Runner | FastAPI＋長時間SSE＋MCP常駐 |
| Langfuse（DB含む） | Google Cloud Run or AWS App Runner | 観測性。自身のDBが別途必要 |
| メインDB | Supabase（Postgres） | SQLite の置換 |
| 大容量ログストレージ | Cloud Storage + BigQuery | アップロードログ/解析ログ |
| 環境変数（フロント） | Vercel シークレット管理 | ビルド時の `VITE_*` のみ |

### 前提の意思決定
- **BigQuery / Cloud Storage が GCP のため、バックエンド＋Langfuse は「Google Cloud Run」に寄せることを推奨。**
  AWS App Runner にする場合、GCP サービスアカウント鍵を AWS 側へ持ち込む形になり、認証・egress が複雑化する。
- 以下は Cloud Run 前提で記載（App Runner でもタスク項目は同一）。

### 現状コードの重要事実（移行に影響）
- UI の `API_BASE` が `http://localhost:8000` に**ハードコード**（`apps/ui/src/App.tsx:23`, `AnalysisHistoryView.tsx:28` ほか複数）
- BigQuery MCP はバックエンドが**子プロセス起動**（`BIGQUERY_MCP_COMMAND` / `BIGQUERY_MCP_ARGS`）
- GCP **サービスアカウント鍵**を使用（`apps/agents/secrets/sa-key.json`）
- 永続化は **SQLite**（`apps/agents/data/results.sqlite3`）
- 解析は **60〜370 秒**の長時間処理＋**SSE**（`text/event-stream`）で進捗ストリーム

---

## 1. メインDB移行（SQLite → Supabase / Postgres）※最重量
- [ ] `storage.py` を sqlite3 → Postgres ドライバ（`psycopg` / SQLAlchemy）へ置換
- [ ] スキーマDDLを Postgres 方言へ（`analysis_history` / `analysis_evaluations` / `answer_scenarios` / `run_history` / configs / questionnaire 等）
      - ※ `analysis_history` は再解析の系譜列 `parent_run_id` / `root_run_id` / `revision` を含む（[docs/plan/reanalysis.md](plan/reanalysis.md)）。DDL 化時に忘れず含めること
- [ ] `ALTER TABLE` ベースのマイグレーション（`axis_assessment_json` 追加、`analysis_history` の系譜3列追加等）を **Alembic 等の正式マイグレーション**に置換
- [ ] `AUTOINCREMENT`→`SERIAL/IDENTITY`、`?` プレースホルダ→`%s`、`json.dumps` 列→`jsonb` 検討
- [ ] コネクションプール（Supabase pooler / pgbouncer）対応
- [ ] 既存ローカルDBの移行要否判断（基本は不要＝新規で開始）
- [ ] Supabase プロジェクト作成（**東京リージョン**）、接続情報を発行

## 2. バックエンドのコンテナ化 & Cloud Run デプロイ
- [ ] `Dockerfile` 作成（Python + uvicorn、`apps/agents`）
- [ ] uvicorn を `$PORT` 待受に（Cloud Run 要件）、`--host 0.0.0.0`
- [ ] **リクエストタイムアウトを最大（最大 3600s）に設定**（解析 370s 対応）
- [ ] **SSE 維持のため min-instances≥1 / CPU always-allocated** を設定（スケール to ゼロだとストリーム切れ・コールドスタート）
- [ ] 同時実行数（concurrency）・メモリ/CPU サイズ決定（重い解析向け）
- [ ] tree-sitter segfault 対策の現状維持（`LOG_ANALYZER_TREE_SITTER` 既定 OFF 確認）

## 3. BigQuery MCP Toolbox の配置
- [ ] MCP 子プロセス起動（`BIGQUERY_MCP_COMMAND` / `ARGS`）がコンテナ内で動くか検証
- [ ] 方式決定：**①バックエンドコンテナに同梱**（同一プロセスツリー）or **②別 Cloud Run サービス** or **③MCP 廃止し BigQuery SDK 直叩きに改修**
- [ ] MCP のハードニング設定（writeMode=blocked 等）を本番でも維持

## 4. Langfuse セルフホスト（Cloud Run + DB）
- [ ] Langfuse バージョン確定：**v2＝Postgres 1 つ / v3＝Postgres＋ClickHouse＋Redis＋S3 互換**の依存を洗い出し
- [ ] Langfuse の DB は Supabase 相乗り or Cloud SQL を選定
- [ ] Langfuse を Cloud Run にデプロイ、公開 URL を発行
- [ ] `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` を新 URL・新キーで再設定
- [ ] （v3 なら）ClickHouse / Redis / GCS バケットのプロビジョニング

## 5. 大容量ログストレージ（Cloud Storage + BigQuery）
- [ ] ログアップロードを **GCS バケットへ保存**する経路に改修（現状 FS/DB 前提を変更）
- [ ] アップロード方式：署名付き URL で直 PUT or バックエンド経由（サイズ方針決定）
- [ ] GCS→BigQuery 取り込み（既存の BQ 解析ルートと接続、テーブル/データセット命名）
- [ ] GCS バケット作成（**東京**）、ライフサイクル（保持/削除）ポリシー
- [ ] 顧客ログのオブジェクト単位アクセス制御

## 6. フロントエンド（Vercel）
- [ ] **`API_BASE` ハードコードを `import.meta.env.VITE_API_BASE` に置換**（`App.tsx:23`, `AnalysisHistoryView.tsx:28` ほか全箇所）
- [ ] Vite の `VITE_API_BASE` を Vercel 環境変数に設定（Cloud Run の URL）
- [ ] Vercel プロジェクト作成、`apps/ui` をルートに（モノレポ設定 / build command `npm run build`, output `dist`）
- [ ] SSE（`EventSource` / fetch stream）がクロスオリジンで動くか確認
- [ ] 独自ドメインなし＝ `*.vercel.app` を使用（CORS 許可元をこのドメインに）

## 7. シークレット / 環境変数
- [ ] **Vercel**：`VITE_API_BASE`（フロントのビルド時のみ。※フロントに秘密は置かない）
      - `VITE_SHOW_EVALUATION` は**設定しない**（未設定＝「解答と比較評価」パネルをマスク）。検証用ビルドでのみ `1` を設定する
- [ ] **Cloud Run（バックエンド）**：`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `LANGFUSE_*` / `BIGQUERY_*` / モデル名各種（`AUDIT_MODEL` / `EVAL_MODEL` / `RALLY_*` 等）/ フラグ（`LOG_COMPACT_ENABLED` 等）→ **Secret Manager** で注入
- [ ] **GCP SA 鍵**：`secrets/sa-key.json` は鍵ファイル配布をやめ、**Cloud Run のサービスアカウントに Workload Identity で権限付与**（BQ / GCS）
- [ ] `.env` をリポジトリから除外確認（`.gitignore`）、`.env.example` を新構成に更新

## 8. 認証・セキュリティ・レート制限 ※公開前必須
- [ ] API に認証（最低限の共有トークン / OAuth）を追加（現状無認証）
- [ ] レート制限（1 解析 200 万トークン超＝乱用で高額）
- [ ] CORS を Vercel ドメインのみ許可に絞る
- [ ] 監査ログ / コスト上限アラート

## 9. ネットワーク / リージョン / CORS
- [ ] Vercel / Cloud Run / Supabase / GCS を **全て東京リージョン**で統一
- [ ] Cloud Run の CORS 設定（`*.vercel.app`）
- [ ] （必要なら）Cloud Run を認証必須にしフロントからのみ許可

## 10. CI/CD・IaC
- [ ] バックエンド：Cloud Build / GitHub Actions で `docker build → Cloud Run deploy`
- [ ] フロント：Vercel の Git 連携（`apps/ui` 自動デプロイ）
- [ ] Supabase マイグレーションのデプロイ手順
- [ ] ステージング / 本番の環境分離

## 11. データプライバシー / コンプラ ※非技術だが最重要
- [ ] 顧客ログ・config を **Vercel / GCP / Supabase（外部クラウド）へ送出することの同意取得**
- [ ] 「ローカル完結」方針からの正式な転換記録（BigQuery に続く 2 回目の転換）
- [ ] データ保持 / 削除ポリシー、アクセス権限、暗号化の明文化

## 12. 動作確認・切替
- [ ] ローカル→本番の 1 解析 E2E（SSE 最後まで / 履歴保存 / 評価 / BQ 取得）
- [ ] 370 秒級の長時間解析がタイムアウトしないか実測
- [ ] Langfuse にトレースが飛ぶか確認
- [ ] 失敗系（保存失敗の可視化）も確認

---

## クリティカルパス（着手順の推奨）
1. **DB移行（#1）** ＝ 最重量・全ての土台
2. **バックエンドのコンテナ化＋Cloud Run（#2, #3）** ＝ SSE / MCP / 実行時間が動くこと
3. **`API_BASE` 環境変数化＋Vercel（#6, #7）** ＝ フロント接続
4. **Langfuse（#4）／大容量ストレージ（#5）**
5. **認証・リージョン・プライバシー（#8, #9, #11）** ＝ 公開前ゲート

### コード改修が確実に発生する主要2点（技術的主工数）
- **#1 SQLite → Supabase(Postgres) 移行**
- **#6 `API_BASE` ハードコード解消（環境変数化）**

その他はインフラ設定が中心。
