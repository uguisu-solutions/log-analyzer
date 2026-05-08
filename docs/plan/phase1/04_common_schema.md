# 04. 共通出力スキーマ v0.1

すべての構成（構成1〜4）が返す唯一の出力契約。Phase 1 で確定し、Phase 2 以降は
互換性を維持したまま機能拡張する（破壊的変更は `schema_version` を上げる）。

定義: [apps/agents/src/log_analyzer/schema.py](../../../apps/agents/src/log_analyzer/schema.py)

---

## 4.1 トップレベル `AnalysisResult`

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `schema_version` | string | `"v0.1"` | スキーマのバージョン |
| `trace_id` | UUID | 自動生成 | Langfuse trace と紐づく ID |
| `config_id` | enum | — | `config1` / `config2` / `config3` / `config4` |
| `input_log_ref` | string | — | 入力ログの参照（S3 URL またはローカルパス） |
| `root_cause_candidates` | array | — | 原因候補（最大3件、rank昇順） |
| `recommended_actions` | array | — | 推奨アクション |
| `confidence` | float | — | 全体の確信度 0.0〜1.0 |
| `agent_trace` | array | `[]` | エージェント呼び出しの内部ツリー（構成3・4で使用） |
| `metrics` | object | デフォルト値 | トークン消費・レイテンシ・圧縮率 |
| `info_loss_flags` | array | `[]` | 構成2 で前処理により情報欠損した可能性の警告 |

## 4.2 `RootCauseCandidate`

| フィールド | 型 | 説明 |
|---|---|---|
| `rank` | int (≥1) | 1 = 最有力 |
| `category` | enum | `FW` / `Net` / `App` / `DNS` / `Sec` / `Unknown` |
| `summary` | string | 原因の要約 |
| `evidence` | string[] | 元ログ行（または抜粋） |

## 4.3 `RecommendedAction`

| フィールド | 型 | 説明 |
|---|---|---|
| `action` | string | 実施すべき行動 |
| `human_judgment_required` | bool | **議事録 L3 対応**。ロールバック・再起動・設定変更・データ削除を伴うものは必ず `true` |
| `risk_level` | enum | `low` / `mid` / `high` |

## 4.4 `Metrics`

| フィールド | 型 | 説明 |
|---|---|---|
| `tokens_in` | int | 入力トークン |
| `tokens_out` | int | 出力トークン |
| `cost_usd` | float | 推定コスト（モデル単価から計算） |
| `latency_ms_p50` | int | レイテンシ p50（構成3・4 で複数 Agent 並列時に使用） |
| `latency_ms_total` | int | 開始〜終了の経過時間 |
| `compression_ratio` | float | 構成2 の圧縮率（前処理後 / 元） |

## 4.5 `TraceNode`（`agent_trace` 要素）

| フィールド | 型 | 説明 |
|---|---|---|
| `node_id` | string | ノードID |
| `parent_id` | string \| null | 親ノードID（オーケストレータ→監視Agentの親子関係） |
| `agent_name` | string | エージェント名（例 `fw-monitor`） |
| `started_at` / `ended_at` | datetime | 実行時刻 |
| `inputs` / `outputs` | object | 任意 JSON |

> `agent_trace` は構成1 では空配列でよい。構成4（ラリー型）の Orchestrator が
> 5監視Agent を呼び出す木構造を表現するために用意してある。

---

## 4.6 サンプル

```json
{
  "schema_version": "v0.1",
  "trace_id": "8e1a3a90-9d2c-4d2e-9c3a-1b8d7d3a4f12",
  "config_id": "config1",
  "input_log_ref": "samples/logs/sample_firewall.log",
  "root_cause_candidates": [
    {
      "rank": 1,
      "category": "FW",
      "summary": "policy v414 で 10.0.40.0/24 → 10.0.20.5 dport=80 を許可するルールが削除されたことによる新規通信のDENY",
      "evidence": [
        "policy v413 -> v414 applied (deleted rule id=4001 src=10.0.40.0/24 dst=10.0.20.5 dport=80)",
        "DENY src=10.0.40.17 dst=10.0.20.5 dport=80 reason=no-match policy=v414"
      ]
    }
  ],
  "recommended_actions": [
    {
      "action": "policy v414 をロールバックして v413 を再適用、または rule id=4001 相当を再投入する",
      "human_judgment_required": true,
      "risk_level": "high"
    },
    {
      "action": "10.0.40.0/24 のクライアント影響範囲を ops-bot のページから特定",
      "human_judgment_required": false,
      "risk_level": "low"
    }
  ],
  "confidence": 0.9,
  "agent_trace": [],
  "metrics": {
    "tokens_in": 1024,
    "tokens_out": 380,
    "cost_usd": 0.012,
    "latency_ms_p50": 4200,
    "latency_ms_total": 4200,
    "compression_ratio": 0.0
  },
  "info_loss_flags": []
}
```

---

## 4.7 互換性ポリシー

| 変更内容 | バージョン |
|---|---|
| フィールド追加（既定値あり） | マイナーバンプ `v0.2` |
| 既存フィールドの意味変更 | メジャーバンプ `v1.0`、構成側の同時更新が必要 |
| 列挙値の追加 | マイナーバンプ |
| 列挙値の削除 | メジャーバンプ |

破壊的変更が必要な場合は、上位計画 [implementation_plan.md §3.1](../implementation_plan.md) を更新したうえで、
`schema.py` のクラス名は維持し、`schema_version` 文字列のみ上げる。
