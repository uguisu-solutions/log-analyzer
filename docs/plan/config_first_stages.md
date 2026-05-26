# Config-First 2 段階解析 — 設計ドキュメント (Phase A)

**作成日**: 2026-05-26
**ブランチ**: `feature/config-first-stages`
**前提**: 既存のトポロジー解析タブ ([feature/topology-analysis](../reports/poc_progress_2026-05-25.md)) が `main` 取り込み待ち。本機能はそこから派生する後続フェーズ。

---

## 1. 背景

2026-05-26 仕様策定議事録より:

> オペレーターの暗黙知であるコンフィグ情報を参照情報に含めることが有効。
> 人の思考プロセス（**構成情報から当たりをつけ、ログで事実確認**）に近い、
> 段階的なアプローチで検証を進める。まずコンフィグ情報で原因の当たりをつけ、
> その後にログを解析する 2 段階プロセスを採用する。
> システム側で、コンフィグ利用のオン/オフ切り替えができるように準備する。

これを満たすため、既存の **「1 ノードに複数ログ + 複数 Config 添付」** を流用しつつ、
解析実行を **Stage 1 (Configs のみで仮説形成) → 人間承認 → Stage 2 (Logs で事実確認)** の
2 段階に分割する独立タブを新設する。

---

## 2. 既存資産との関係

既存トポロジー解析タブ（`feature/topology-analysis` ブランチ・現在 `main` 直前）から流用するもの:

| 流用元 | 用途 |
|---|---|
| `TopologyAnalysis.tsx` の画像 + 矩形描画 | そのまま再利用（同じ UX で構成図を作る）|
| `NodeEditor` / `AttachmentSection` (ログ・Config 添付 UI) | そのまま再利用 |
| `_build_topology_log_text` (api.py) | 「configs のみで合成」「logs + 仮説 で合成」の 2 経路で使い分け |
| `run_rally_stream` (rally_agent.py) | 各 Stage 内部の rally として 2 回呼ぶ |
| `DelegationHistoryView` / `GraphView` | Stage 別に表示 |
| `_PENDING_DECISIONS` / `_APPEND_QUEUES` (api.py) | 既存の確認モーダル機構を流用、新アクション `advance` / `abort` を追加 |

新設するもの:

- `apps/agents/src/log_analyzer/rally_two_stage.py` — 2 段階オーケストレータ
- `POST /api/runs/config-first-stream` — 新エンドポイント（既存 `/api/runs/topology-stream` は変更せず残す）
- `AnalysisResult.stage_outputs: list[StageOutput]` — Stage 別中間結果（schema_version v0.1 据置、空配列フォールバック）
- `apps/ui/src/ConfigFirstAnalysis.tsx` — 新タブ本体
- App.tsx の `Mode` に `'config-first'` 追加

**既存トポロジー解析タブには手を入れない** — 1 段階モードはそちらで継続提供、本タブは 2 段階モード専用。

---

## 3. 2 段階フロー

```
[ユーザー]
   │ 構成図画像アップロード → ノード矩形描画
   │ 各ノードに configs と logs を添付
   │
   ▼
[Config-First 解析タブ: 実行]
   │
POST /api/runs/config-first-stream
   { config, topology, node_logs, node_configs, rally_max_rounds, questionnaire? }
   │
   ▼
================ Stage 1: Configs のみで仮説形成 ================
   │ log_text = _build_topology_log_text(topology, node_logs={}, node_configs=...)
   │   ※ logs は空辞書 (Stage 1 では渡さない)
   │
   ▼
run_rally_stream(..., topology_context={stage: "config"})
   │
   ▼ SSE: stage=1 を含む既存イベント (orchestrator_decision / monitor_decision /
   │       integrator_done) + 新イベント stage_one_complete
   │
   ▼
[人間承認モーダル — 必須]
   │ Stage 1 で出た suspected_nodes と仮説 summary を表示
   │ "ログで事実確認に進む" / "ここで停止"
   │
POST /api/runs/{run_id}/decision  {action: "advance" | "abort"}
   │
   ├── abort  → そのまま final emit (stage_outputs に Stage 1 のみ含む)
   │
   └── advance → ↓
   ▼
================ Stage 2: Logs で事実確認 ================
   │ stage_1_hypothesis_block = """
   │   ## Stage 1 仮説 (コンフィグ解析より)
   │   - fw-01 [primary]: policy reload で lb-to-app-01 が欠落の疑い
   │   - lb-01 [secondary]: app-01 のヘルスチェック失敗が予測される
   │ """
   │ log_text = stage_1_hypothesis_block
   │          + _build_topology_log_text(topology, node_logs=..., node_configs=...)
   │
   ▼
run_rally_stream(..., topology_context={stage: "log", prior_hypothesis: [...]})
   │
   ▼ SSE: stage=2 を含む同様のイベント + stage_two_complete
   │
   ▼
================ 最終 final ================
   final イベント:
     AnalysisResult {
       confidence: ...,
       root_cause_candidates: [...],     # Stage 2 の integrator 出力
       recommended_actions: [...],
       suspected_node_ids: [...],         # Stage 2 で確認されたもの
       suspected_node_findings: [...],
       delegation_history: [...],         # Stage 2 のもの
       stage_outputs: [                   # ★ NEW
         {stage: "config", suspected_node_ids, suspected_node_findings,
          delegation_history, confidence, summary},
         {stage: "log",    suspected_node_ids, suspected_node_findings,
          delegation_history, confidence, summary}
       ]
     }
```

### 重要な設計判断

- **Stage 1 でも Stage 2 でも既存の `run_rally_stream` をそのまま使う**。プロンプトは変えない。
  - Stage 1 は logs が無いので integrator は自然に「config からの推定」を出す
  - Stage 2 は仮説ブロックが log_text の冒頭に入るので、integrator は自然に「仮説の検証」モードになる
  - こうすることで監視 / integrator のプロンプトは触らず、テスト負荷を最小化
- **2 段階の orchestrator 状態は完全に分離**。Stage 2 では新しい trace_id / 新しい delegation_history が始まる
  - Langfuse 上は親 trace 1 + 子 trace 2 のネスト構造にする（後続最適化、Phase A では別 trace でも可）
- **Stage 間の hypothesis 受け渡しは log_text への前置で行う**
  - state を直接渡すよりプロンプト経由の方が LLM の理解が安定する
  - prompt caching への影響: Stage 2 の log_text は仮説ブロックが先頭で可変なので、その部分はキャッシュ対象から外れる
- **必須モーダル**: 議事録「人の思考プロセスに近い」に忠実。自動進行モードは作らない（オペレーターを必ず一度通す）

---

## 4. スキーマ拡張

`apps/agents/src/log_analyzer/schema.py` に追加:

```python
class StageOutput(BaseModel):
    """Config-First 2 段階解析の各 Stage の中間結果。

    1 段階目 (stage="config"): コンフィグ情報のみで推定された仮説
    2 段階目 (stage="log"):    ログで事実確認した結果
    """

    stage: str  # "config" | "log"
    stage_label: str = ""  # 表示用ラベル ("Stage 1: コンフィグ解析" 等)
    confidence: float = 0.0
    summary: str = ""  # Stage 単位の総評
    suspected_node_ids: list[str] = Field(default_factory=list)
    suspected_node_findings: list[SuspectedNodeFinding] = Field(default_factory=list)
    delegation_rounds: int = 0
    delegation_history: list[DelegationEventDTO] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)
    trace_id: str = ""  # Stage 単位の Langfuse trace


class AnalysisResult(BaseModel):
    # ... 既存フィールド ...
    # 既存 stage 系フィールド (delegation_history 等) は **最終 stage** のものを継続して入れる
    # (1 段階モードとの互換維持)。stage_outputs は両 stage 分の詳細を保持
    stage_outputs: list[StageOutput] = Field(default_factory=list)
```

schema_version は **v0.1 据置** (追加フィールドのみで破壊変更なし)。

---

## 5. SSE イベント仕様（新規 + 既存流用）

| event kind | 発火タイミング | data |
|---|---|---|
| `run_id_assigned` | 既存。最初に 1 回 | `{run_id}` |
| `stage_one_start` | **新**。Stage 1 の rally を開始する直前 | `{stage_label}` |
| `run_started` 〜 `integrator_done` | 既存。Stage 1 中に流れる。各イベントの data に `stage: "config"` を含めて UI 側で区別 | （既存） |
| `stage_one_complete` | **新**。Stage 1 終了、人間承認待ち。Stage 1 hypothesis をペイロードに含む | `{stage_output: StageOutput}` |
| `user_decision` | 既存。`{action: "advance" \| "abort"}` を含む | `{action, ...}` |
| `stage_two_start` | **新**。advance 選択後、Stage 2 開始直前 | `{stage_label, prior_hypothesis_summary}` |
| `run_started` 〜 `integrator_done` | 既存。Stage 2 中。各イベント data に `stage: "log"` 含む | （既存） |
| `final` | 既存。最終 AnalysisResult。`stage_outputs` 入り | `{result: AnalysisResult}` |
| `error` | 既存 | `{stage, message}` |

---

## 6. 決定 API 拡張

`POST /api/runs/{run_id}/decision`:

- 既存: `{action: "continue" \| "stop", extend_by?: int}` — rally_max_rounds 上限到達時の延長 / 停止
- **追加**: `{action: "advance" \| "abort"}` — Config-First の Stage 1→2 承認 / 中断
- 4 アクションを 1 エンドポイントで受け、`rally_two_stage` 側のキューが適切に解釈する

---

## 7. UI 設計（新タブ）

タブ名: **「Config-First 解析」**

```
┌─ Config-First 解析 ───────────────────────────────────────┐
│ [ヘッダ + 説明]                                            │
│ ┌─ ステージインジケータ ─────────────────────────────────┐ │
│ │  ① Configs 解析  →  人間承認  →  ② Logs 検証  → 完了   │ │
│ │    [●進行中]        [待ち]       [未]         [未]       │ │
│ └────────────────────────────────────────────────────────┘ │
│ [ツールバー: 画像選択 / 編集モード切替 / クリア]            │
│ ┌─ キャンバス ─────────────┐ ┌─ サイドバー ─────────────┐ │
│ │ 構成図 + ノード矩形       │ │ NodeEditor                │ │
│ │ (ハイライト: 各 Stage の  │ │ (logs/configs 添付 -      │ │
│ │  suspected_node_ids)      │ │  既存 UI 流用)             │ │
│ └──────────────────────────┘ └──────────────────────────┘ │
│ [実行バー: 構成セレクト / rally_max_rounds / 実行]          │
│                                                            │
│ [Stage 1 ライブログ (SSE)]                                 │
│ [Stage 1 完了 → 承認モーダル ★]                            │
│ [Stage 2 ライブログ (SSE)]                                 │
│                                                            │
│ ┌─ 最終結果 ─────────────────────────────────────────────┐ │
│ │ [Tab: 統合 | Stage 1 | Stage 2]                          │ │
│ │  - 統合: 最終 AnalysisResult (Stage 2 結果)              │ │
│ │  - Stage 1: configs のみで出た仮説                       │ │
│ │  - Stage 2: logs で確認された結果                        │ │
│ │  各 Tab に suspected_nodes + delegation_history          │ │
│ └────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
```

### 承認モーダル仕様

Stage 1 完了で表示される必須モーダル:
- タイトル: 「コンフィグ解析が完了しました — ログ検証に進みますか？」
- Stage 1 の suspected_nodes を severity 別カードで表示
- Stage 1 の root_cause_candidates / confidence
- ボタン:
  - **「ログ検証に進む」** (primary) — `advance` を送信
  - **「ここで終了」** (secondary) — `abort` を送信

abort 時も AnalysisResult は返るが、Stage 2 部分は空。

### ノード矩形のハイライト切り替え

Stage 1 ライブログ中 → Stage 1 の suspected_nodes でハイライト
Stage 2 ライブログ中 → Stage 2 の suspected_nodes でハイライト
最終結果表示時 → 結果ペインで選択中の Tab の suspected_nodes に追従

---

## 8. テスト計画

### バックエンド単体テスト (`tests/test_config_first_api.py`)

| テスト | 検証 |
|---|---|
| `test_two_stage_request_validation` | mode 必須・config4 ベースのみ受け付け |
| `test_stage_outputs_populated_after_advance` | advance 選択時、stage_outputs に 2 件入る |
| `test_stage_outputs_only_stage_one_on_abort` | abort 選択時、stage_outputs に 1 件のみ |
| `test_stage_1_log_text_excludes_node_logs` | Stage 1 の合成 log_text に logs が含まれないこと |
| `test_stage_2_log_text_includes_hypothesis` | Stage 2 の合成 log_text に Stage 1 仮説ブロックが先頭に来ること |
| `test_decision_advance_abort` | decision エンドポイントが新アクション 2 種を受ける |

### 動作確認シナリオ

既存の [samples/topology/scenario1_lb_fw_denial/](../../samples/topology/scenario1_lb_fw_denial/) をそのまま流用:
- Stage 1 (configs のみ): fw-policy.conf の policy reload を読んで「fw-01 と lb-01 が怪しい」と仮説形成
- 人間承認 → advance
- Stage 2 (logs): 実際の deny ログと lb のヘルスチェック失敗ログで仮説確定

将来は `fw-policy.conf` 等のサンプル設定ファイルを充実させる（Phase A の範囲外）。

---

## 9. 後続フェーズへの接続

本ドキュメントは Phase A のみカバー。Phase B〜F は後続に分離。

| Phase | 内容 | 本機能との関係 |
|---|---|---|
| G | `RootCauseCandidate.rank` 撤去（schema v0.2） | Phase A 完了直後に着手 |
| B | 問診票機能 | `POST /api/runs/config-first-stream` に `questionnaire_answers` を追加（既に payload には予約済み） |
| C | GPT 監査エージェント | Stage 2 完了後の独立段階として追加 |
| D | ラウンド集計ビュー | 既に `StageOutput.metrics` を持つので可視化のみ |
| E | チャット形式 UI | 本タブの UI レイアウトを会話スレッド型に再構成 |
| F | 問診票有無の比較ベンチマーク | `compare_configs.py` 拡張 |

---

## 10. 既知の留意事項

- **uvicorn のリロード**: コード変更後手動再起動（既知の罠）
- **prompt caching への影響**: Stage 2 の log_text 先頭に可変の仮説ブロックが入るので、その部分はキャッシュヒットしない。Stage 1 と Stage 2 は別 system prompt として扱われる
- **Anthropic API コスト**: 1 回の解析で 2 回 rally するため、トークン消費は単一モードの約 2 倍。検証目的では許容範囲
- **abort 時の表示**: 最終結果 Tab は「Stage 1 のみ」を表示、「統合」「Stage 2」は無効化
- **ブラウザ閉じた時の挙動**: SSE が切れると Stage 中断扱い。状態復元は MVP では未対応（リトライ前提）
