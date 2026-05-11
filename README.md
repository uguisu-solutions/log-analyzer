# log-analyzer

ネットワーク／システムインフラのログを LLM で分析する **マルチエージェント検証プラットフォーム** の PoC。

> 目的: 「単純な単一 LLM」「フィルタ前処理 + 圧縮」「複数モデル並列 + 統合」「オーケストレータ駆動の動的ラリー」「ユーザー定義パイプライン」の **5 構成を同じ入出力契約のまま並列実行・比較**することで、運用ログの根本原因分析にどの構成パターンがどの程度効くかを評価する。

---

## 概要

| 構成 | 名称 | 概要 |
|---|---|---|
| **config1** | ベースライン | ログ → Claude Sonnet 1 回 → 共通 JSON |
| **config2** | フィルタ + 圧縮 | ルールベース前処理 → Haiku triage → Sonnet analyze |
| **config3** | マルチモデル並列 | Sonnet / Haiku / GPT-4o の 3 モデル並列 → integrate |
| **config4** | オーケストレータ駆動ラリー | LangGraph の orchestrator が監視結果を見て **再評価ループ**。focus_hints で観点を変えながら最大 3 ラウンド回し integrate |
| **config5** | ユーザー定義パイプライン | UI で input → ... → output の DAG をドラッグ＆ドロップ構築 |

すべての構成が **同一の出力スキーマ `AnalysisResult` (schema_version v0.1)** を返すため、機械突合・比較表化が可能です。

### 主要機能

- 5 構成の単独実行 / 同時比較（Web UI + CLI 両対応）
- ノード単位のプロンプト・モデル上書き編集 + ユーザー定義構成として SQLite に保存
- React Flow ベースの **ワークフロー可視化**（builtin 構造 + 構成5 の D&D 編集）
- **config4 の判断履歴可視化**（各ラウンドの action / focus_hints / rationale をタイムライン表示）
- **ログ管理タブ**: アップロード / プレビュー / 削除
- Langfuse による全 LLM 呼び出しのトレース・トークン消費記録

---

## 技術スタック

### バックエンド (`apps/agents/`)

| 領域 | 採用 |
|---|---|
| 言語 | Python 3.11+ |
| エージェント orchestration | [LangGraph](https://github.com/langchain-ai/langgraph)（StateGraph）|
| LLM | Anthropic Claude (Sonnet 4.5 / Haiku 4.5 / Opus 4.7) + OpenAI (GPT-4o) |
| Web フレームワーク | FastAPI + Uvicorn |
| スキーマ | Pydantic v2 |
| 永続化 | SQLite（ユーザー定義構成）+ ローカル FS（ログ・トポロジ） |
| 観測性 | [Langfuse](https://langfuse.com/) v2（OSS LLMOps、Docker Compose で同梱） |
| テスト | pytest |

> **AWS 不採用方針**: Step Functions / Bedrock / DynamoDB / S3 は使用しません。Python asyncio + LangGraph + Anthropic/OpenAI 直叩き + SQLite + ローカル FS で代替しています。

### フロントエンド (`apps/ui/`)

| 領域 | 採用 |
|---|---|
| フレームワーク | React 19 + TypeScript |
| ビルド | Vite 5 |
| ワークフロー描画 | [React Flow](https://reactflow.dev/) v11 |
| 通信 | fetch API（外部 HTTP クライアントなし） |

---

## ディレクトリ構造

```
prottype1/
├── apps/
│   ├── agents/                 # Python バックエンド + 4 構成の実装 + FastAPI
│   │   ├── src/log_analyzer/
│   │   │   ├── schema.py              # 共通出力スキーマ AnalysisResult
│   │   │   ├── baseline_agent.py      # config1
│   │   │   ├── filtered_agent.py      # config2
│   │   │   ├── multi_model_agent.py   # config3
│   │   │   ├── rally_agent.py         # config4（orchestrator 駆動）
│   │   │   ├── rally/                 # config4 のサブモジュール
│   │   │   ├── pipeline_runner.py     # config5（DAG 実行エンジン）
│   │   │   ├── prompt_slots.py        # 編集可能 slot 定義
│   │   │   ├── storage.py             # SQLite 永続化
│   │   │   ├── api.py                 # FastAPI エンドポイント
│   │   │   └── cli.py                 # `log-analyze` CLI
│   │   ├── scripts/compare_configs.py # 複数構成 × 複数ログ一括比較
│   │   └── tests/                     # pytest（36 件）
│   └── ui/                     # React フロントエンド
│       └── src/
│           ├── App.tsx                # タブ管理 / 単一実行 / 比較 / 構成設計 / ログ管理
│           ├── BuiltinConfigCanvas.tsx
│           ├── PipelineBuilder.tsx    # 構成5 D&D エディタ
│           ├── GraphView.tsx          # 実行後のエージェント組織図
│           └── LogManager.tsx         # ログアップロード / プレビュー / 削除
├── infra/langfuse/             # Langfuse v2 docker-compose
├── samples/
│   ├── logs/                   # サンプル合成ログ（FW / Routing / TCP 異常等）
│   └── topology/               # config4 の read_topology ツール用モック
└── docs/
    ├── plan/                   # 実装計画
    └── reports/                # 進捗レポート
```

---

## セットアップ

### 前提

- Python 3.11+（3.13 推奨）
- Node.js 20+ / npm 10+
- Docker Desktop（Langfuse 用）
- Anthropic API キー（必須） / OpenAI API キー（config3 を使う場合）

### 1. リポジトリ取得

```powershell
git clone <this repo>
cd prottype1
```

### 2. Langfuse 起動

```powershell
cd infra\langfuse
Copy-Item .env.example .env
# .env の SECRET / SALT を任意の値に更新
docker compose up -d
# http://localhost:3000 を開く
```

初回起動後、Langfuse UI でアカウント作成 → **Settings → API Keys** で
Public / Secret キーを発行 → 次の手順の `.env` に貼ります。

### 3. バックエンドのセットアップ

```powershell
cd apps\agents
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 環境変数
Copy-Item .env.example .env
# .env を編集:
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...      （config3 を使う場合のみ）
#   LANGFUSE_PUBLIC_KEY=...    （Langfuse UI で発行）
#   LANGFUSE_SECRET_KEY=...
#   LANGFUSE_HOST=http://localhost:3000
```

### 4. フロントエンドのセットアップ

```powershell
cd apps\ui
npm install --legacy-peer-deps
# Vite 5 + React 19 の peer dep が一部ズレているため --legacy-peer-deps が必要
```

---

## 起動手順

3 つのターミナルで並行起動します。

```powershell
# Terminal 1: Langfuse（既に動いていればスキップ）
cd infra\langfuse
docker compose up -d

# Terminal 2: FastAPI バックエンド
cd apps\agents
.\.venv\Scripts\Activate.ps1
uvicorn log_analyzer.api:app --port 8000
# 起動後: http://localhost:8000/api/configs で構成一覧が返る

# Terminal 3: Vite フロントエンド
cd apps\ui
npm run dev
# 起動後: http://localhost:5173 を開く
```

> **注意**: uvicorn は `--reload` を渡していません。バックエンドコードを変更したら手動で Ctrl+C → 再起動してください。

---

## 使い方

### Web UI（推奨）

`http://localhost:5173` を開くと 4 タブが表示されます。

| タブ | 用途 |
|---|---|
| **単一実行** | ログを 1 つ選び、構成を指定して分析実行 → 結果を表示。config4 専用のラリー制御パネル（max_rounds / 強制最小ラウンド）あり |
| **構成比較** | 複数構成を同じログに同時実行 → 確信度・トークン・レイテンシを並べて比較 |
| **構成設計（pipeline）** | 構成5 を D&D で設計 → ユーザー定義構成として保存 |
| **ログ管理** | `samples/logs/` 配下のログを一覧 / アップロード（10 MB 上限）/ 先頭 200 行プレビュー / 削除 |

ノードをクリックすると **プロンプト・モデルをその場で編集** でき、実行前に試したり、ユーザー定義構成として別名保存できます。

### CLI

```powershell
cd apps\agents
.\.venv\Scripts\Activate.ps1

# 構成1（既定）
log-analyze ..\..\samples\logs\sample_firewall.log

# 構成4 をラリー上限 2 ラウンドで実行
log-analyze --config config4 --rally-max-rounds 2 ..\..\samples\logs\sample_firewall.log

# 強制最小ラウンドで再入を確実に観測（PoC デモ用）
log-analyze --config config4 --rally-force-min-rounds 2 ..\..\samples\logs\sample_firewall.log
```

### 一括比較

```powershell
cd apps\agents
.\.venv\Scripts\Activate.ps1

# 全 builtin 構成 × 全サンプルログ
python scripts\compare_configs.py ..\..\samples\logs\*.log

# ユーザー定義構成も含めて CSV 出力
python scripts\compare_configs.py ..\..\samples\logs\*.log --include-user --csv out.csv
```

---

## API エンドポイント（抜粋）

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/api/configs` | builtin + ユーザー定義構成一覧 |
| GET | `/api/logs` | ログ一覧（ファイル名 / 行数 / バイト / mtime） |
| POST | `/api/logs` | ログアップロード（multipart, 10 MB 上限, .log のみ） |
| GET | `/api/logs/{name}/content` | ログ先頭 200 行プレビュー |
| DELETE | `/api/logs/{name}` | ログ削除 |
| GET | `/api/configs/{base}/structure` | builtin 構成のグラフ構造（React Flow 描画用） |
| GET | `/api/prompt-slots/{base_config}` | 編集可能 slot 定義 |
| GET / POST / PUT / DELETE | `/api/configs/saved[/{id}]` | ユーザー定義構成の CRUD |
| POST | `/api/runs` | 指定構成で実行 → `AnalysisResult` を返す |

---

## テスト

```powershell
cd apps\agents
.\.venv\Scripts\Activate.ps1
pytest -q
# 期待: 36 passed
```

---

## 設計上の固定ポイント

- **`AnalysisResult` が単一の出力契約**。新しい構成を足すときもサブクラスではなく `config_id` を切り替えて同じ型を返す。
- **`human_judgment_required` は外せない**。ロールバック・再起動・設定変更を伴うアクションは必ず `true` を立てる（議事録 L3 由来）。
- **トレース名は `<config_id>-<role>` に揃える**（例: `config4-orchestrator`、`config4-fw-monitor`）。Langfuse でフィルタしやすくするため。
- **言語は日本語が既定**。プロンプト・出力 `summary` / `action` の自然文は日本語、フィールド名・enum 値は英語。

---

## 既知の留意事項

- **uvicorn のリロード**: コード変更時は手動再起動。`--reload` で起動すれば自動化できる
- **Vite ピア依存**: `npm install --legacy-peer-deps` が必要（Vite 5 + React 19）
- **Anthropic API 残高**: PoC 開発・テストで継続的に消費するため、長期休止後は <https://console.anthropic.com/settings/billing> を確認
- **ポート**: 3000 (Langfuse) / 8000 (FastAPI) / 5173 (Vite)。衝突する場合は各 compose / uvicorn / vite の引数で変更可能
- **Langfuse の DB パスワード不整合**: `langfuse-server` が `P1000 Authentication failed` で再起動ループする場合、ボリューム内の Postgres パスワードと `.env` の `LANGFUSE_DB_PASSWORD` がズレている。修復:
  ```powershell
  # .env に書いてあるパスワードを <pw> として
  docker exec langfuse-db psql -h 127.0.0.1 -U langfuse -d langfuse -c "ALTER USER langfuse WITH PASSWORD '<pw>';"
  ```
  `docker compose down -v` でボリュームをクリーンする方法もあるが、過去の trace 履歴を失う

---

## ライセンス

PoC 段階のため未設定。

---

## 関連ドキュメント

- [docs/plan/implementation_plan.md](docs/plan/implementation_plan.md) — 全体実装計画
- [apps/agents/README.md](apps/agents/README.md) — バックエンド詳細
- [infra/README.md](infra/README.md) — Langfuse セットアップ詳細
