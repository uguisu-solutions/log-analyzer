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
| **config4** | 委譲チェーン型ラリー | orchestrator が初手の監視 1 つを選ぶ→各監視が分析後に次ノード（別監視 or integrator）を JSON で指名する **シングルアクティブな委譲チェーン**。SSE でリアルタイム可視化、上限到達時はユーザー確認モーダルで延長 / 停止を選択 |
| **config5** | ユーザー定義パイプライン | UI で input → ... → output の DAG をドラッグ＆ドロップ構築 |

すべての構成が **同一の出力スキーマ `AnalysisResult` (schema_version v0.1)** を返すため、機械突合・比較表化が可能です。

### 主要機能

- 5 構成の単独実行 / 同時比較（Web UI + CLI 両対応）
- ノード単位のプロンプト・モデル上書き編集 + ユーザー定義構成として SQLite に保存
- React Flow ベースの **ワークフロー可視化**（builtin 構造 + 構成5 の D&D 編集）
- **config4 のリアルタイム委譲チェーン可視化** — SSE で「初手選択 → 監視 A → 監視 B → integrator」を 1 ラウンドずつ UI に流す
- **ラウンド数上限到達時の確認モーダル** — 続行 (+N 延長) / 停止 (即 integrator) をユーザーが選択
- **遷移制約**: 自己遷移と直前ノードへの即時 ping-pong を禁止（違反時は自動 integrator フォールバック）
- **ログ管理タブ**: アップロード / プレビュー / 削除
- **実行履歴タブ**: SQLite に各実行のメタデータ（confidence / tokens / Langfuse trace_id）を残し、フィルタ表示
- Langfuse による全 LLM 呼び出しのトレース・トークン消費記録 + UI 直リンク
- **Prompt caching**: orchestrator / 監視 / integrator の system プロンプトと安定 user ブロックに `cache_control: ephemeral` を設定し、連続実行で 2 回目以降の入力 token を最大 90% 削減

---

## 技術スタック

### バックエンド (`apps/agents/`)

| 領域 | 採用 |
|---|---|
| 言語 | Python 3.11+ |
| エージェント orchestration | 構成3 は `asyncio.gather` で並列。構成4 は **手動 async ループ** + SSE ストリーミング (LangGraph は旧 fan-out 型で使用していたが委譲チェーン型では不要に)。構成5 は依存深度ごとに `asyncio.gather` で並列実行する DAG ランナー |
| LLM | Anthropic Claude (デフォルト: orchestrator/監視 = Haiku 4.5、integrator = Sonnet 4.5、Opus 4.7 へ slot 別上書き可) + OpenAI (GPT-4o-mini、構成3 のみ) |
| Web フレームワーク | FastAPI + Uvicorn（SSE 用に `StreamingResponse`） |
| スキーマ | Pydantic v2 |
| 永続化 | SQLite（ユーザー定義構成 / 実行履歴）+ ローカル FS（ログ・トポロジ） |
| 観測性 | [Langfuse](https://langfuse.com/) v2（OSS LLMOps、Docker Compose で同梱） |
| テスト | pytest（47 件） |

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
│   │   │   ├── rally_agent.py         # config4（委譲チェーン型・SSE ストリーミング対応）
│   │   │   ├── rally/                 # config4 サブモジュール
│   │   │   │   ├── orchestrator.py      #   初回 1 回のみ初手監視を選択
│   │   │   │   ├── monitors.py          #   各監視 (fw/routing/app/dns/sec) は分析 + 次ノード指名
│   │   │   │   ├── integrator.py        #   最終統合 (Sonnet)
│   │   │   │   ├── tools.py             #   read_topology / get_config モック
│   │   │   │   └── state.py             #   委譲制御 TypedDict
│   │   │   ├── pipeline_runner.py     # config5（DAG 実行エンジン）
│   │   │   ├── prompt_slots.py        # 編集可能 slot 定義
│   │   │   ├── storage.py             # SQLite 永続化（saved_configs / run_history）
│   │   │   ├── api.py                 # FastAPI エンドポイント（SSE 含む）
│   │   │   └── cli.py                 # `log-analyze` CLI
│   │   ├── scripts/compare_configs.py # 複数構成 × 複数ログ一括比較
│   │   └── tests/                     # pytest（47 件）
│   └── ui/                     # React フロントエンド
│       └── src/
│           ├── App.tsx                # タブ管理 / 単一実行 / 比較 / 構成設計 / ログ管理 / 実行履歴
│           │                          #   + RealtimeStreamView / ConfirmationModal / DelegationHistoryView
│           ├── BuiltinConfigCanvas.tsx
│           ├── PipelineBuilder.tsx    # 構成5 D&D エディタ
│           ├── GraphView.tsx          # 実行後のエージェント組織図
│           ├── LogManager.tsx         # ログアップロード / プレビュー / 削除
│           └── RunHistoryView.tsx     # 実行履歴の一覧 + フィルタ
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

`http://localhost:5173` を開くと 5 タブが表示されます。

| タブ | 用途 |
|---|---|
| **単一実行** | ログを 1 つ選び、構成を指定して分析実行 → 結果を表示。config4 では SSE で各ラウンドをリアルタイム表示 + ラリー制御パネル（最大ラウンド数）+ 上限到達時の確認モーダル（+N 延長 / 停止選択） |
| **構成比較** | 複数構成を同じログに同時実行 → 確信度・トークン・レイテンシを並べて比較 |
| **構成設計（pipeline）** | 構成5 を D&D で設計 → ユーザー定義構成として保存 |
| **ログ管理** | `samples/logs/` 配下のログを一覧 / アップロード（10 MB 上限）/ 先頭 200 行プレビュー / 削除 |
| **実行履歴** | SQLite に蓄積した過去実行を構成 / ログ / 部分文字列でフィルタ表示 + Langfuse 直リンク |

ノードをクリックすると **プロンプト・モデルをその場で編集** でき、実行前に試したり、ユーザー定義構成として別名保存できます。

### CLI

```powershell
cd apps\agents
.\.venv\Scripts\Activate.ps1

# 構成1（既定）
log-analyze ..\..\samples\logs\sample_firewall.log

# 構成4 を委譲チェーン上限 2 ラウンドで実行
log-analyze --config config4 --rally-max-rounds 2 ..\..\samples\logs\sample_firewall.log
```

CLI からは確認モーダルは出ず、上限到達で強制 finalize されます（対話的に延長したい場合は Web UI を使ってください）。

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
| POST | `/api/runs` | 指定構成で実行 → `AnalysisResult` を返す（同期） |
| **POST** | **`/api/runs/stream`** | **構成4 を SSE で実行（`text/event-stream`）。各ステップを 1 イベントずつ push** |
| **POST** | **`/api/runs/{run_id}/decision`** | **SSE 中の `await_confirmation` への応答。`{"action": "continue", "extend_by": N}` か `{"action": "stop"}`** |
| GET | `/api/runs/history` | 実行履歴一覧（フィルタ・ページング対応） |
| GET / DELETE | `/api/runs/history/{run_id}` | 個別実行の取得・削除 |
| GET | `/api/runtime-config` | UI 初期化用（Langfuse host 等） |

### SSE イベント kind（構成4 のみ）

| kind | 意味 |
|---|---|
| `run_id_assigned` | 確認モーダル応答に使う `run_id` を返す |
| `run_started` | trace_id と rally_max_rounds を通知 |
| `orchestrator_start` / `orchestrator_decision` | 初手の監視を選択中 / 選択結果 |
| `monitor_start` / `monitor_decision` | 監視ノードの実行開始 / findings + 次ノード指名 |
| `await_confirmation` | 上限到達。UI が確認モーダルを表示 |
| `user_decision` | ユーザー応答（`continue` + `extend_by` / `stop`） |
| `max_rounds_finalize` | 強制 finalize（非対話モードのみ） |
| `integrator_start` / `integrator_done` | 統合中 / 統合完了 |
| `final` | `AnalysisResult` 全体を payload に含む |
| `error` | エラー発生（stage を含む） |

---

## テスト

```powershell
cd apps\agents
.\.venv\Scripts\Activate.ps1
pytest -q
# 期待: 47 passed
```

---

## 設計上の固定ポイント

- **`AnalysisResult` が単一の出力契約**。新しい構成を足すときもサブクラスではなく `config_id` を切り替えて同じ型を返す。
- **`human_judgment_required` は外せない**。ロールバック・再起動・設定変更を伴うアクションは必ず `true` を立てる（議事録 L3 由来）。
- **トレース名は `<config_id>-<role>` に揃える**（例: `config4-orchestrator`、`config4-fw-monitor`）。Langfuse でフィルタしやすくするため。
- **言語は日本語が既定**。プロンプト・出力 `summary` / `action` の自然文は日本語、フィールド名・enum 値は英語。
- **構成4 はシングルアクティブな委譲チェーン**。複数監視を同時に走らせない（自己遷移と直前ノードへの即時 ping-pong は禁止）。並列ファンアウト案は 2026-05-14 に廃止。

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
