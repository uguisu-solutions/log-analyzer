# log-analyzer (Phase 1 baseline agent)

Phase 1 で立ち上げる **構成1（単純LLM）** ベースラインの Python 実装。
すべての構成が共通の出力スキーマ `AnalysisResult`（`schema_version: v0.1`）を返す前提で、
構成2〜4 は本パッケージのスキーマと Langfuse trace 構造を再利用する。

## クイックスタート

```powershell
# 1. 依存解決（uv 推奨。pip でも可）
cd apps\agents
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2. 環境変数
Copy-Item .env.example .env
# .env に ANTHROPIC_API_KEY と LANGFUSE_* を設定

# 3. 単体テスト
pytest

# 4. サンプルログで実行
log-analyze ..\..\samples\logs\sample_firewall.log              # 構成1（既定）
log-analyze --config config2 ..\..\samples\logs\sample_firewall.log  # 構成2
```

実行後、Langfuse UI（既定 `http://localhost:3000`）の **Traces** に
`config1-baseline` または `config2-filtered` が現れ、Generation・トークン消費・
最終 JSON を確認できる。構成2 は Haiku triage と Sonnet analyze の 2 段の
Generation がぶら下がる。

## 関連ドキュメント

- [BigQuery ログ取得ルート（Google 公式 MCP 経由）](docs/bigquery_mcp.md) —
  大容量ログを BigQuery から都度取得する構成のセットアップ・設計・安全性
  （toolbox の入手、`config/bigquery_toolbox.yaml` のハードニング、writeMode 検証など）。

## ディレクトリ構造

```
apps/agents/
├── pyproject.toml
├── .env.example
├── docs/
│   └── bigquery_mcp.md    # BigQuery 取得ルート（MCP 経由）の手順・設計
├── config/
│   └── bigquery_toolbox.yaml  # toolbox の custom config（SELECT 限定等のハードニング）
├── scripts/
│   └── ingest_logs_to_bq.py   # ローカルログを BigQuery へ投入（前段・直叩き）
├── src/log_analyzer/
│   ├── schema.py          # 共通出力スキーマ v0.1（Pydantic）
│   ├── tracing.py         # Langfuse クライアント
│   ├── baseline_agent.py  # 構成1：log -> Sonnet -> 共通JSON
│   ├── filters.py         # 構成2 第1段：ルールベースフィルタ
│   ├── filtered_agent.py  # 構成2：log -> filter -> Haiku triage -> Sonnet -> 共通JSON
│   ├── bigquery_client.py # BigQuery 取得（SELECT 組み立て＋MCP 実行）/ 投入クライアント
│   ├── bigquery_mcp.py    # BigQuery MCP（toolbox）への stdio クライアント
│   └── cli.py             # `log-analyze [--config configN]` エントリポイント
└── tests/
    ├── test_schema.py
    ├── test_filters.py
    ├── test_bigquery_client.py
    └── test_bigquery_mcp.py
```

## 設計上の固定ポイント（Phase 2 以降も維持）

- **AnalysisResult が単一の出力契約**。構成2〜4 を追加する時はサブクラスではなく
  `config_id` を切り替えて同じ型を返す。
- **`human_judgment_required` は外せない**。構成4 のオーケストレータも、
  ロールバック・再起動・設定変更を伴うアクションは必ず `true` を立てる。
- **トレース名は `<config_id>-<role>` に揃える**。例: `config1-baseline`、
  `config4-orchestrator`、`config4-fw-monitor`。比較画面でフィルタしやすい。
