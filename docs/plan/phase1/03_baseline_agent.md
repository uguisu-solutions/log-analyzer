# 03. ベースラインエージェント（構成1）の実装と実行

Phase 1 完了基準 **C3（Trace 可視化）** を満たすための、構成1（単純LLM）の
実装をここで解説する。

---

## 3.1 構成1 のスコープ

```
[ログテキスト] → [Anthropic Claude Sonnet 4.5] → [共通スキーマ JSON]
                          ↓
                    [Langfuse trace]
```

- **入力**: 単一のログテキスト（数百〜数千行を想定、200KB 上限）
- **モデル**: Claude Sonnet 4.5（環境変数 `BASELINE_MODEL` で差し替え可）
- **出力**: [共通出力スキーマ v0.1](04_common_schema.md) の `AnalysisResult`
- **副作用**: Langfuse へ trace 送信（trace 名 `config1-baseline`、内側に
  `claude-sonnet-4-5` という Generation を1つ持つ）

---

## 3.2 ファイル構成

| パス | 役割 |
|---|---|
| [src/log_analyzer/schema.py](../../../apps/agents/src/log_analyzer/schema.py) | Pydantic 共通スキーマ |
| [src/log_analyzer/tracing.py](../../../apps/agents/src/log_analyzer/tracing.py) | Langfuse クライアントの初期化 |
| [src/log_analyzer/baseline_agent.py](../../../apps/agents/src/log_analyzer/baseline_agent.py) | 構成1 本体 `run_baseline()` |
| [src/log_analyzer/cli.py](../../../apps/agents/src/log_analyzer/cli.py) | `log-analyze` CLI |
| [tests/test_schema.py](../../../apps/agents/tests/test_schema.py) | スキーマ単体テスト |

---

## 3.3 セットアップ

```powershell
cd apps\agents
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

Copy-Item .env.example .env
# .env を編集して以下を埋める
#   ANTHROPIC_API_KEY        Anthropic コンソールから
#   LANGFUSE_PUBLIC_KEY      Langfuse UI から（Settings → API Keys）
#   LANGFUSE_SECRET_KEY      同上
#   LANGFUSE_HOST            既定 http://localhost:3000 のままでよい

pytest
```

`pytest` は API キー不要のスキーマ単体テストのみを通す。3 件パスすれば配線 OK。

---

## 3.4 実行

```powershell
log-analyze ..\..\samples\logs\sample_firewall.log
```

標準出力に `AnalysisResult` の JSON（`schema_version: v0.1`）が出る。
同時に Langfuse UI（<http://localhost:3000>）の **Tracing → Traces** を開くと、
`config1-baseline` という trace が現れる。

trace を開くと中に：

- 入力: ログサイズと参照
- 出力: `AnalysisResult` の全文
- 子要素 `claude-sonnet-4-5`（Generation）: モデル入出力・トークン数

が記録される。これで完了基準 **C3** が達成される。

---

## 3.5 プロンプト設計（System Prompt）

[baseline_agent.py](../../../apps/agents/src/log_analyzer/baseline_agent.py) の
`SYSTEM_PROMPT` 定数に置いた。要点は4つ：

1. **JSON-only 出力**: 後段の Pydantic パースを単純化
2. **候補は最大3件**: 比較画面のフォーマット安定性
3. **`human_judgment_required` の必須化**: 議事録 L3（自動実行の危険）への対応
4. **証拠 (`evidence`) として元ログ行を引用**: 後の評価で機械突合できる

> プロンプトを変更すると構成1 の精度が変わるため、Phase 2 以降で評価する際は
> Langfuse の **Prompt Versioning** に登録してバージョン管理する。Phase 1 では
> ハードコードのままでよい（時期尚早の最適化を避ける）。

---

## 3.6 構成2〜4 を載せる時の拡張点

ベースラインの構造は構成2〜4 でもそのまま使える。差分は以下のみ：

| 構成 | `run_*()` で増える処理 |
|---|---|
| 構成2 (前処理あり) | 入力直後にフィルタ→Haiku圧縮を挟む。`metrics.compression_ratio` を埋める |
| 構成3 (マルチモデル) | Step Functions Parallel で複数モデルを並列。最後に統合エージェント |
| 構成4 (ラリー型) | LangGraph の StateGraph に置き換え、5監視Agent + Orchestrator を表現 |

すべて **同じ `AnalysisResult` を返す** ことが規約。`config_id` を切り替えるだけで
比較画面に乗る。
