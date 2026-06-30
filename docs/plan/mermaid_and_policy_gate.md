# 実装プラン：Mermaid 構成図入力 ＋ 解析方針の事前確認ゲート

> 対象：config-log 解析タブ。実装はこのプラン確定後に着手する。
> 関連：[config_log_stages.md](./config_log_stages.md) / 推論フロー（orchestrator → monitors → integrator → audit）。

> **状態（2026-07 実装完了）**: Phase 1・Phase 2 とも実装済み。確定仕様は
> [config_log_stages.md §0「第5次改修」](./config_log_stages.md) に集約。
> 本プラン本文は計画時のもので、以下「実装での確定差分」が優先される。

## 実装での確定差分（計画 → 実装）

| 項目 | 計画（本文） | 実装での確定 |
|---|---|---|
| `require_policy_approval` 既定 | サーバ既定 `True` | サーバ既定 **`False`**（安全側）。ON 既定はフロントのトグル初期値 `true` で担保。バッチは常に `false` |
| Mermaid 注入位置 | 「## トポロジー要約」直後 | リンク一覧の直後・ノード別添付の前（`_build_topology_log_text` 内） |
| `topology_context["mermaid_source"]` | 追加する | **追加していない**（mermaid は `log_text` 注入のみで一貫させた） |
| プランナー計測の載せ先 | `round_metrics` に「planner」ロール | **`AnalysisResult.policy_proposal`** に計測ごと保持（reasoningReport / ChatHistoryView で表示）。round_metrics は変更なし |
| 方針ブロック注入の実装場所 | `rally_two_stage.py` | **api.py ハンドラ側**（`_gen_single` / `_gen_two_stage` で `log_text` 先頭に連結）。rally_two_stage.py は無変更 |
| 併載の堅牢化 | （プラン外） | BQ 取得結果の行数/文字数上限（tools.py）＋ 監視 JSON 再生成 1 回（monitors.py）を同時に実装。詳細は config_log_stages.md §0 第5次改修 |

## 確定した設計判断
1. **Mermaid の役割**：テキスト文脈として `log_text` に注入するのみ（パースして nodes/links 化はしない）。ノード対応は LLM のベストエフォート。図ハイライト連携は対象外（将来課題）。
2. **方針確認ゲートの既定**：実行バーにトグルを置き、**初期状態 ON**。確認不要時のみ外す。
3. **まとめて実行（バッチ）時**：方針ゲートは**自動承認でスキップ**（提案は記録するがモーダルでは止めない）。

---

## 機能1：Mermaid 構成図の入力（テキスト投入のみ）

### データモデル
- `TopologyDef`（[types.ts:348](../../apps/ui/src/types.ts#L348)）に `mermaid?: string` を追加。
  - localStorage 永続化・解析履歴保存・PNG出力で使い回されるため、永続化と履歴再現が自動で乗る。
  - [topologyImage.ts](../../apps/ui/src/topologyImage.ts) は追加フィールドを無視するので非破壊。

### フロントエンド（[ConfigLogAnalysis.tsx](../../apps/ui/src/ConfigLogAnalysis.tsx)）
- 構成図エリア（topology-header 付近）に「Mermaidで記述」セクションを追加：
  - `<textarea>`（貼り付け）＋ファイルアップロード（`.mmd` / `.txt` / `.md`）＋ドラッグ&ドロップ。
  - `setTopology(t => ({ ...t, mermaid }))` で保存。
- `loadTopology` / `saveTopology`（[:99-117](../../apps/ui/src/ConfigLogAnalysis.tsx#L99-L117)）に `mermaid` を含める。
- 送信ボディ（[:586-604](../../apps/ui/src/ConfigLogAnalysis.tsx#L586-L604)）に `mermaid: topology.mermaid ?? null` を追加。

### バックエンド
- `ConfigLogRunRequest`（[api.py:1615](../../apps/agents/src/log_analyzer/api.py#L1615)）に `mermaid: str | None = None`。
- `_build_topology_log_text`（[api.py:533](../../apps/agents/src/log_analyzer/api.py#L533)）に `mermaid` 引数を追加し、「## トポロジー要約」直後に
  ```
  ## ネットワーク構成図 (Mermaid)
  <mermaid 原文>
  ```
  ブロックを挿入。→ orchestrator（log_text のみ参照）含む全エージェントが参照可能になる。
- `topology_context`（[api.py:1722-1735](../../apps/agents/src/log_analyzer/api.py#L1722-L1735)）に `"mermaid_source": mermaid` を追加。

### 受け入れ条件
- mermaid を入力して解析すると、推論過程・integrator の根拠で構成図情報が反映されうる（プロンプト上で参照される）。
- mermaid 未入力でも従来通り動作（後方互換）。

---

## 機能2：解析方針の事前確認ゲート

### 概要
解析開始直後・orchestrator の**前**に「方針プランナー」を1回実行 → 方針を提案 → SSE `policy_proposal` → `_PENDING_DECISIONS[run_id]` の Future で承認/中止を待つ → 承認後に本解析。既存の `decision_waiter` 機構（[api.py の _PENDING_DECISIONS / /decision エンドポイント](../../apps/agents/src/log_analyzer/api.py#L1963)）を再利用する。

### バックエンド
1. **新モジュール `rally/planner.py`**（[orchestrator.py](../../apps/agents/src/log_analyzer/rally/orchestrator.py) と同型）
   - `PLANNER_PROMPT`：mermaid＋log＋config＋問診票（＝`log_text` 全体）から方針を構造化 JSON で出力。
     ```json
     {
       "situation_summary": "現象の要約",
       "primary_hypotheses": ["想定原因の方向性", "..."],
       "investigation_plan": ["起点と調査順序", "..."],
       "suggested_first_node": "fw|routing|app|dns|sec",
       "focus": "最初に当てる観点",
       "data_to_use": ["使用する入力データ"],
       "missing_data_notes": "不足データ・前提"
     }
     ```
   - 入力は `log_text`（mermaid 注入済み）。`plan_policy(state) -> dict`（model / tokens / latency も返す）。
2. **ゲートの設置場所＝config-log-stream ハンドラ**（[api.py:1657](../../apps/agents/src/log_analyzer/api.py#L1657)、`run_rally_stream` / `run_two_stage_stream` 呼び出しの**前**、once）
   - 2段階は内部で run_rally_stream を2回呼ぶため、ゲートを rally_agent 内に置くと二重発火する → **stage ロジックの上（ハンドラ）に置く**。
   - フロー：
     ```
     run_id_assigned
       → policy_start
       → (plan_policy 実行)
       → policy_proposal { proposal: {...} }
       → [バッチ以外 & トグルON] await _wait_for_decision(run_id)
       → user_decision { action: approve_policy | reject_policy, edited_focus? }
       → reject なら final(aborted) で終了
       → approve なら本解析へ
     ```
   - 承認された方針を **`log_text` 先頭に「## 承認済み解析方針」ブロックとして追記**してから run_rally_stream / run_two_stage_stream に渡す。
     - 2段階は stage1 / stage2 双方の log_text に注入（[stage_one_log_text](../../apps/agents/src/log_analyzer/api.py#L1862) と `_stage_two_log_text`）。
   - `edited_focus` があればそれを方針ブロックの focus として使う。
3. **`DecisionRequest`**（[api.py:327](../../apps/agents/src/log_analyzer/api.py#L327)）にアクション追加：
   - `approve_policy` / `reject_policy`、任意 `edited_focus: str | None`。
4. **`ConfigLogRunRequest`** に `require_policy_approval: bool = True`（トグル ON 既定）。
   - **バッチ実行はフロント側で `false` を送る**（自動スキップ）。サーバは false なら policy_proposal を emit（記録目的）しつつ await せず即進行。

### フロントエンド（[ConfigLogAnalysis.tsx](../../apps/ui/src/ConfigLogAnalysis.tsx)）
- 実行バー（[audit トグル:871-876](../../apps/ui/src/ConfigLogAnalysis.tsx#L871-L876) と同型）に「解析方針を確認する」トグル（state 初期値 true）。
- 送信ボディに `require_policy_approval: 単発=トグル値 / バッチ=false` を追加。
  - 単発 `run()` は state のトグル値、`runBatch()` は固定 false（[runOne の opts](../../apps/ui/src/ConfigLogAnalysis.tsx#L542) に渡す形が素直）。
- 新モーダル `PolicyProposalModal.tsx`（[ConfirmationModal](../../apps/ui/src/ConfirmationModal.tsx) を踏襲）：
  - 方針 JSON を整形表示。ボタン：**［この方針で解析］／［観点を修正して解析］（edited_focus 入力）／［中止］**。
- SSE 処理（[runOne の switch:615-664](../../apps/ui/src/ConfigLogAnalysis.tsx#L615-L664)）に `policy_start` / `policy_proposal` ケースを追加：
  - `policy_proposal` で `pendingPolicy` state にセット → モーダル表示。`user_decision` で閉じる。
- `submitDecision`（[:716](../../apps/ui/src/ConfigLogAnalysis.tsx#L716)）を拡張し `approve_policy` / `reject_policy`（＋ edited_focus）を `/api/runs/{runId}/decision` に送信。
- ライブチャット表示（[LiveChatView](../../apps/ui/src/LiveChatView.tsx)）でも `policy_proposal` を会話として表示できるよう [renderEventSummary] に対応を追加。

### SSE プロトコル追加
| イベント | data | emit 箇所 |
|---|---|---|
| `policy_start` | `{ model_hint }` | api.py ハンドラ |
| `policy_proposal` | `{ proposal: {...上記JSON} }` | api.py ハンドラ |
| （流用）`user_decision` | `{ action, edited_focus? }` | /decision 経由 |
| （中止時）`final` | `{ result: aborted 相当 }` | api.py ハンドラ |

---

## エッジケース / 留意点
- **decision_waiter の再アーム**：rally_max_rounds は同一 run で複数回 await する実装。方針ゲートはその前に1回 await を足すだけだが、`_wait_for_decision` が await ごとに Future を張り直しているか実装時に要確認（張り直していなければ方針ゲート用に最初の await を追加するだけで OK）。
- **プランナーのコスト/計測**：LLM 1コール増。token_log/round_metrics に「planner」ロールとして載せる（レポート整合のため推奨）。`reasoningReport.ts` / `RoundMetricsView` 側の表示ロール対応も確認。
- **2段階の既存 advance/abort ゲート**（`require_approval`、現状OFF）とは別物。方針ゲートは stage1 の更に前。
- **中止（reject_policy）時の UI**：`stageStatus` を `aborted` にして「解析方針が却下されました」を表示。履歴保存はしない。
- **後方互換**：`require_policy_approval=false` かつ `mermaid=null` で従来挙動と完全一致になること。

---

## フェーズ分割
- **Phase 1**：機能1（mermaid をテキスト投入）。型追加＋フォーム＋log_text 注入。小さく安全。
- **Phase 2**：機能2（方針プランナー＋確認ゲート＋トグル＋モーダル＋SSE）。
- **Phase 3（任意・将来）**：mermaid パース→nodes/links 化、構成図ハイライト連携。

## 変更ファイル一覧
| 区分 | ファイル | 内容 |
|---|---|---|
| 型 | apps/ui/src/types.ts | `TopologyDef.mermaid`、SSE/Decision 型の追記 |
| FE | apps/ui/src/ConfigLogAnalysis.tsx | Mermaid フォーム / 方針トグル / SSE 処理 / submitDecision 拡張 |
| FE | apps/ui/src/PolicyProposalModal.tsx（新規） | 方針提案モーダル |
| FE | apps/ui/src/LiveChatView.tsx ほか | policy_proposal の会話表示 |
| BE | apps/agents/src/log_analyzer/api.py | リクエスト型 / _build_topology_log_text / ハンドラのゲート / DecisionRequest |
| BE | apps/agents/src/log_analyzer/rally/planner.py（新規） | 方針プランナー（PLANNER_PROMPT / plan_policy） |
| BE | apps/agents/src/log_analyzer/rally_two_stage.py | stage1/stage2 log_text への方針ブロック注入（必要に応じて） |
