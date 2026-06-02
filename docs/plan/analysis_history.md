# 解析履歴タブ — 仕様 & DB 設計

**作成日**: 2026-06-02
**ブランチ**: `feature/config-log-analysis`
**関連**: [config_log_stages.md](config_log_stages.md)（config-log 解析本体）

---

## 1. 目的

config-log 解析の各実行について、**入力・Claude の推論過程・結果**を DB に保存し、
専用の「**解析履歴**」タブから

1. 過去の解析を**一覧**で確認し、
2. 個々の履歴を選ぶと**解析終了後の画面の状態を再現**（構成図ハイライト + 会話形式の
   推論過程 + 最終結果）

できるようにする。

既存の「実行履歴」タブ ([RunHistoryView](../../apps/ui/src/RunHistoryView.tsx) /
`run_history` テーブル) は **全構成横断の軽量メタデータ + Langfuse 直リンク**の用途で
据え置く。本機能はそれとは別に、config-log 解析に限定した**完全再現用の重いレコード**を
別テーブルに持つ（関心の分離。run_history は肥大化させない）。

---

## 2. 「再現」に必要なデータ

解析終了後 (chat 表示) の画面は次から構成される。これを再現するために保存する:

| 画面要素 | 必要データ | 保存先 |
|---|---|---|
| 構成図キャンバス + ノード矩形 + severity ハイライト | `topology` (image/nodes/links) + `result.suspected_node_findings` | request_json / result_json |
| 会話スレッド（人間 → orchestrator → 監視 → integrator → 監査） | `result.delegation_history` / `stage_outputs` / `round_metrics` / `audit_report` + `questionnaire_answers` | result_json / request_json |
| 最終結論（根本原因候補 / 推奨アクション / 確信度） | `result.root_cause_candidates` / `recommended_actions` / `confidence` | result_json |
| 実行モード表示 | `analysis_mode` / `single_source` / `stage_order` | request_json + 専用列 |

> ノード添付の生ログ・設定 (`node_logs` / `node_configs`) は**保存しない**。解析後画面には
> 表示されず、サイズ・機微情報の観点でも持たない方針（再現は画面状態に限定。再実行用途は対象外）。
> 構成図画像 (dataURL) は再現に必要なので保存する（サイズは §6 の留意点参照）。

「Claude の推論過程」は `result.delegation_history`（最終 Stage）と各
`stage_outputs[].delegation_history` / `round_metrics` に含まれ、
[ChatHistoryView](../../apps/ui/src/ChatHistoryView.tsx) がそこから会話を再構成する。
これは解析中のライブ表示 (LiveChatView) と同じ素材で、**解析後 chat 画面と等価**。

---

## 3. DB 設計

`apps/agents/data/results.sqlite3` に新テーブルを追加（既存テーブルは変更なし）。

```sql
CREATE TABLE IF NOT EXISTS analysis_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,                 -- ストリームの run_id
    created_at    TEXT NOT NULL,                 -- ISO8601 UTC
    kind          TEXT NOT NULL DEFAULT 'config-log',
    config_id     TEXT NOT NULL,
    analysis_mode TEXT,                           -- "single" | "two_stage"
    single_source TEXT,                           -- "config" | "log" | "both"
    stage_order   TEXT,                           -- "config_log" | "log_config"
    -- 一覧表示・フィルタ用サマリ (result から抽出)
    title         TEXT,                           -- 見出し (top_summary 等)
    confidence    REAL,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    latency_ms    INTEGER,
    top_category  TEXT,
    top_summary   TEXT,
    trace_id      TEXT,
    -- 完全再現用 JSON
    request_json  TEXT NOT NULL,                  -- §4 参照
    result_json   TEXT NOT NULL                   -- AnalysisResult 全体
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_history_run ON analysis_history(run_id);
CREATE INDEX IF NOT EXISTS idx_analysis_history_created ON analysis_history(created_at DESC);
```

- `run_id` は UNIQUE。同一 run の二重 POST は **no-op**（最初の 1 件のみ保持）。
- 一覧 API はサマリ列のみ返し、`request_json` / `result_json` は**詳細 API でのみ**返す
  （一覧の転送量を抑える）。

---

## 4. request_json の形

```jsonc
{
  "config_id": "config4",
  "analysis_mode": "two_stage",
  "single_source": "both",
  "stage_order": "config_log",
  "rally_max_rounds": 3,
  "view_mode": "chat",
  "questionnaire_answers": { "symptom_onset": "...", "...": "..." },
  "topology": {
    "image": "data:image/png;base64,...",   // 構成図 (dataURL)
    "imageWidth": 800, "imageHeight": 580,
    "nodes": [ { "id": "fw-01", "type": "FW", "label": "", "ip": "...", "x": .., "y": .., "w": .., "h": .. } ],
    "links": [ { "source": "fw-01", "target": "lb-01" } ]
  }
}
```

---

## 5. API

| メソッド | パス | 用途 |
|---|---|---|
| POST | `/api/analysis-history` | 解析完了時に 1 件保存（フロント駆動）。重複 run_id は no-op |
| GET | `/api/analysis-history` | 一覧（サマリのみ、`limit` / `offset` / `kind` / `analysis_mode` / `q` フィルタ）|
| GET | `/api/analysis-history/{id}` | 個別取得（request / result の完全 JSON 込み）|
| DELETE | `/api/analysis-history/{id}` | 削除 |

**POST リクエスト** (`AnalysisHistorySaveRequest`):
```jsonc
{
  "run_id": "...", "kind": "config-log", "config_id": "config4",
  "analysis_mode": "...", "single_source": "...", "stage_order": "...",
  "rally_max_rounds": 3, "view_mode": "chat",
  "questionnaire_answers": {...},
  "topology": {...},          // image 込み
  "result": { /* AnalysisResult */ }
}
```
バックエンドが `result` から `confidence` / `tokens_in/out` / `latency_ms` /
`top_category` / `top_summary` / `trace_id` / `title` を抽出して列に格納。

**保存タイミング**: フロントは config-log 解析の SSE で `final` を受けた直後に POST する
（完了した解析のみが履歴に残る）。

---

## 6. UI

新タブ **「解析履歴」** (`App.tsx` の `Mode` に `'analysis-history'`)。
コンポーネント [AnalysisHistoryView.tsx](../../apps/ui/src/AnalysisHistoryView.tsx):

- **一覧ビュー**: テーブル（日時 / モード / 確信度 / tokens / レイテンシ / top / 操作）。
  フィルタ（モード・テキスト）+ 再読み込み + 削除。行クリックで詳細へ。
- **詳細ビュー**（解析後画面の再現）:
  - 「← 一覧へ戻る」+ メタ情報（日時・モード・trace リンク・tokens 等）
  - **構成図キャンバス**（画像 + ノード矩形 + severity ハイライト）= 解析後の構成図状態を再現
  - **ChatHistoryView**（`result` + `questionnaire_answers`）= 推論過程 + 最終結果を会話形式で再現。
    **2 段階解析では `result.stage_outputs` を Stage ごとに展開**し、各 Stage の委譲チェーン
    （Claude の推論過程）と結論をそれぞれ表示する（最終 Stage だけでなく Stage 1 の推論も再現）。
  - **RoundMetricsView**（ラウンド別 tokens/latency、`result.round_metrics`）

詳細は読み取り専用。再実行はしない（画面状態の再現に限定）。

---

## 7. テスト

`apps/agents/tests/test_analysis_history.py`:
- 保存 → 一覧 → 個別取得 → 削除の往復
- 重複 run_id が no-op になること
- result からサマリ列が抽出されること
- 一覧レスポンスに `request_json` / `result_json` が含まれない（軽量）こと

---

## 8. 留意点

- **画像サイズ**: 構成図 dataURL（最大 5MB base64）を request_json に持つため、件数が増えると
  SQLite が肥大化する。PoC では許容。将来は画像を別ストア / 参照に分離する余地あり。
- **run_history との重複**: config-log 実行は run_history（軽量・横断）と analysis_history
  （完全・config-log 限定）の両方に記録される。用途が異なるため許容。
- **保存失敗は本流を壊さない**: 履歴保存の POST 失敗は解析結果表示を妨げない
  （best-effort、エラーはコンソール / 控えめな通知に留める）。
