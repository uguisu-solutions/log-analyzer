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

## ディレクトリ構造

```
apps/agents/
├── pyproject.toml
├── .env.example
├── src/log_analyzer/
│   ├── schema.py          # 共通出力スキーマ v0.1（Pydantic）
│   ├── tracing.py         # Langfuse クライアント
│   ├── baseline_agent.py  # 構成1：log -> Sonnet -> 共通JSON
│   ├── filters.py         # 構成2 第1段：ルールベースフィルタ
│   ├── filtered_agent.py  # 構成2：log -> filter -> Haiku triage -> Sonnet -> 共通JSON
│   └── cli.py             # `log-analyze [--config configN]` エントリポイント
└── tests/
    ├── test_schema.py
    └── test_filters.py
```

## 設計上の固定ポイント（Phase 2 以降も維持）

- **AnalysisResult が単一の出力契約**。構成2〜4 を追加する時はサブクラスではなく
  `config_id` を切り替えて同じ型を返す。
- **`human_judgment_required` は外せない**。構成4 のオーケストレータも、
  ロールバック・再起動・設定変更を伴うアクションは必ず `true` を立てる。
- **トレース名は `<config_id>-<role>` に揃える**。例: `config1-baseline`、
  `config4-orchestrator`、`config4-fw-monitor`。比較画面でフィルタしやすい。
