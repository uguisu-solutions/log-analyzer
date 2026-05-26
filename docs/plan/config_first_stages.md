# Config-First 2 段階解析 — 設計ドキュメント (Phase A + A.5 + G + B + C + D + E + F)

**作成日**: 2026-05-26
**更新日**:
  - 2026-05-26 — A.5 Configs ON/OFF トグル追記
  - 2026-05-26 — Phase G (rank 撤去 + schema v0.2) と Phase B (問診票) を追記
  - 2026-05-26 — Phase C (GPT 監査) / D (ラウンド集計) / E (チャット表示) / F (問診票有無比較) を追記
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

## 9.5. Phase A.5: Configs 利用 ON/OFF トグル

議事録「**システム側で、コンフィグ利用のオン/オフ切り替えができるように準備する**」に対応。
タブ内のラジオボタンで以下の 2 モードを切り替え可能:

| モード | 動作 | 必須入力 |
|---|---|---|
| **ON** (既定) | Configs → 人間承認 → Logs の 2 段階 (Phase A 標準) | 各ノードに Config 1 件以上 |
| **OFF** (Skip) | Stage 1 と人間承認をスキップ、**Logs のみ**で 1 段階 rally | 各ノードに Log 1 件以上 |

### バックエンド差分

- `ConfigFirstRunRequest.skip_config_stage: bool = False` を追加
- エンドポイントで分岐:
  - 入力検証: skip 時は Config 不要、代わりに Log 必須
  - skip 時は `run_two_stage_stream` をバイパスし、`run_rally_stream` を直接 1 回呼ぶ
  - log_text は `_build_topology_log_text(topology, node_logs, {})` (configs は含めない、意図に忠実)
  - 新 SSE イベント `stage_one_skipped` を emit (UI はこれで Stage 1 表示をスキップ扱いに)
  - 最終 AnalysisResult の `stage_outputs` は 1 件 (stage="log") のみ

### SSE シーケンス (skip モード)

```
run_id_assigned
stage_one_skipped              ← 新イベント (Stage 1 を飛ばしたことを通知)
stage_two_start                ← Stage 2 単段の開始 (実質は 1 段階目)
(run_rally_stream events, stage="log")
final                          ← stage_outputs = [stage="log"] 1 件のみ
```

### UI 差分

- 「Configs 利用」セレクタ (ラジオ 2 択) を実行バー上部に追加
- StageIndicator は skip 時に Stage 1 + 人間承認を「(skip)」表示
- canRun: skip 時は Log 必須、通常時は Config 必須
- 承認モーダルは skip 時は出ない (decision_waiter が呼ばれないため)

### 検証ユースケース

議事録の **「精度・速度・コスト」3 軸評価** で `Configs あり vs なし` の比較が
同タブ内で完結する。同じシナリオを `ON` と `OFF` で順に実行し、
suspected_node_ids の一致率 / confidence / tokens / latency_ms_total を突き合わせる。

---

## 9.6. Phase G: rank 撤去 + schema v0.2

議事録「解析結果は複数（ランキング形式ではなく）表示する」に対応。

| 項目 | 変更 |
|---|---|
| `RootCauseCandidate.rank` | **削除** (Pydantic デフォルトで extra=ignore のため旧データ互換) |
| `AnalysisResult.schema_version` | `v0.1` → `v0.2` (default) |
| 各構成 (config1〜5) のプロンプト | `rank 1` 等の順位言及を削除、「並列扱い」と明記 |
| UI | `<ol class="candidates">` → `<ul class="candidates candidates-grid">` のグリッド表示、rank バッジ撤去 |

旧データを読む経路 (run_history 等) では `rank` フィールドが残っている可能性があるが、
Pydantic のデフォルト挙動 (extra fields 無視) と UI 側の optional 型 (`rank?: number`)
で互換維持。新規データには rank フィールド自体が含まれない。

---

## 9.7. Phase B: 問診票機能

議事録「問診票を基にエージェントが実行でき、途中での要望差し込みも可能」に対応。

### スキーマ

```python
class QuestionnaireItem:
    key: str        # 答えの辞書 key (system prompt にも露出)
    label: str      # 表示ラベル
    type: str       # "text" | "textarea" | "choice"
    options: list[str]    # type=="choice" の選択肢
    placeholder: str
    required: bool

class QuestionnaireTemplate:
    id: int
    name: str            # ユニーク。"default" は削除不可
    description: str
    items: list[QuestionnaireItem]
    created_at, updated_at
```

### SQLite テーブル

```sql
questionnaire_templates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

`init_db()` 時に `name='default'` のテンプレを idempotent に投入する。
議事録合意の 5 項目 (`symptom_onset` / `scope` / `reproducibility` / `recent_changes` / `free_notes`) を含む。

### CRUD エンドポイント

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/api/questionnaires` | 一覧 |
| GET | `/api/questionnaires/{id}` | 個別取得 |
| POST | `/api/questionnaires` | 新規作成 |
| PUT | `/api/questionnaires/{id}` | 更新 (items 差し替え) |
| DELETE | `/api/questionnaires/{id}` | 削除 (`default` は 400) |

### 実行リクエストへの注入

`TopologyRunRequest` と `ConfigFirstRunRequest` に `questionnaire_answers: dict[str, str] = {}` を追加。
`_build_topology_log_text` の最先頭に `## 問診票回答` ブロックを差し込む形で LLM に渡す:

```
## 問診票回答 (人間オペレータからの一次申告)
- **symptom_onset**: 2026-05-26 09:00 頃から
- **scope**: 特定ユーザー
- **recent_changes**: 前日 18:30 に fw-01 のポリシ更新
...

## トポロジー要約
...
```

Config-First では Stage 1 / Stage 2 双方の log_text に同じ問診票ブロックが入る。
LLM は **最初に「人間が言ったこと」を読んでから** configs / logs に進む構造。

### UI

新コンポーネント `QuestionnairePanel.tsx` を作成し、トポロジー解析タブと
Config-First 解析タブの両方で同じ部品を再利用:

- 折りたたみ可能 (`<details>` 要素、既定: 折りたたみ)
- テンプレ選択ドロップダウン (`default` を含む)
- type 別レンダリング: `text` → input, `textarea` → textarea, `choice` → select
- 回答件数バッジ ("3/5" 等)
- 「回答をクリア」ボタン

回答は **揮発状態** (実行のたびに毎回入力)。永続化は意図的に省略
(機微情報が誤って残ることを避けるため)。テンプレ管理 UI (作成 / 編集モーダル) は
本フェーズでは API のみ提供、UI 追加は将来 (Phase F の比較ベンチマーク作業中に
「問診票あり vs なし」を切り替えやすくする際に必要になれば実装)。

---

## 9.8. Phase C: GPT 監査エージェント

議事録「監査エージェント (GPT想定)」に対応。Claude 系で動いた rally の結論を
**独立した別モデル (GPT-4o-mini)** でポストホック検証する補助段階。

### スキーマ
```python
class AuditReport:
    verdict: str  # "agree" | "partial" | "disagree" | "uncertain"
    confidence: float
    summary: str
    concerns: list[str]
    alternative_hypotheses: list[str]
    model: str
    tokens_in / tokens_out / latency_ms

class AnalysisResult:
    ...
    audit_report: AuditReport | None = None
```

### バックエンド
- `audit_agent.py` 新設: `run_audit(log_text, topology_context, analysis_result)` を提供
  - 既定モデル: `gpt-4o-mini` (`AUDIT_MODEL` 環境変数で上書き可)
  - `OPENAI_API_KEY` 未設定 / API エラー時は `verdict="uncertain"` で本流を壊さない
  - safe_extract_json + 規定外 verdict の正規化
- rally_agent.py: `run_rally_stream(audit_after_integrator=True)` で integrator 後に audit を 1 回実行
  SSE: `audit_start` → `audit_done`
- rally_two_stage.py: 最終 result に対して 1 回だけ `_attach_audit()` を呼ぶ
  (Stage 1 では audit せず、abort 経路なら Stage 1 結果に対して audit)
- api.py: `TopologyRunRequest` / `ConfigFirstRunRequest` に
  `audit_after_integrator: bool` を追加し、3 エンドポイントすべてに伝播

### UI
- `AuditReportView.tsx` 新規: verdict 別配色
  (agree=緑 / partial=黄 / disagree=赤 / uncertain=灰)
  + summary + concerns + alternative_hypotheses
- 両タブの実行バーに「GPT 監査も実行」チェックボックス
- 結果ペイン (Topology / Config-First 統合タブ) に AuditReportView 配置

---

## 9.9. Phase D: ラウンド単位リソース消費

議事録「ラウンド履歴、消費トークン、処理時間をラウンド単位で閲覧可能にする」
に対応。

### スキーマ
```python
class RoundMetrics:
    round: int    # 0=orchestrator, 1..=監視, 最終=integrator
    role: str
    model: str
    tokens_in / tokens_out / latency_ms

class AnalysisResult:
    ...
    round_metrics: list[RoundMetrics] = []

class StageOutput:
    ...
    round_metrics: list[RoundMetrics] = []
```

### バックエンド
- rally_agent.py: `_build_round_metrics(token_log)` で
  orchestrator(round=0) → 各監視(round 順) → integrator(最終 round+1) に並べ直し
- rally_two_stage.py: 各 Stage の `_result_to_stage_output` で取り込み、
  最終 result では両 Stage を直列連結

### UI
- `RoundMetricsView.tsx` 新規: テーブル + バー表示
  tokens は青バー、latency は緑バー (各 round 単位、最大値で正規化)
- 両タブの解析ワークフロー直下に配置
- Config-First では Stage 別タブ / 統合タブいずれにも表示

---

## 9.10. Phase E: チャット形式 結果表示

議事録「UI: チャット形式を想定し、問診票の要求や回答結果を表示する」に対応。
既存の AnalysisResult を「人間 → orchestrator → 各監視 → integrator → 監査」
の会話スレッドとしてレンダリングする**読み取り専用ビュー**。

### バックエンド
変更なし。既存の delegation_history + result + audit_report + Phase B の
問診票回答を UI 側でのみ再構成する。

### UI
- `ChatHistoryView.tsx` 新規: メッセージ単位レンダラ
  - sender 別配色 (human=青 / agent=黄 / integrator=ピンク / audit=緑)
  - 各メッセージに speaker / round タグ / model · tokens · latency メタ
- `ViewModeToggle.tsx` 新規: 「標準 / チャット表示」のラジオ切替を両タブで共用
- 結果ペイン上部にトグルを配置、chat 選択時は ChatHistoryView のみ表示
  標準モードは従来の ResultTabs / Stage 別タブを保持

### 後続検討
- 実行中の会話 (SSE 進行中) もチャット表示する案 → 現在は完了後の result のみ対象
- ユーザーが追加メッセージを送信できる導線 (既存 `append-log` を流用) → 未実装

---

## 9.11. Phase F: 問診票あり/なし比較ベンチマーク CLI

議事録「問診票（指標）の有無両条件で同一シナリオを評価し、スコアリング、
ラウンド数、精度、速度を網羅する」に対応。

### scripts/benchmark_questionnaire.py
- シナリオディレクトリを規約ベースで読み込み
  ```
  <scenario_dir>/
      <node-id>.conf    → node_configs
      <node-id>.log     → node_logs
      questionnaire.json (任意。無ければデフォルト 5 項目を使う)
  ```
- 名前 prefix からノード type を推定 (`fw-`→FW / `lb-`→LB / `api-`→Server 等)
- `questionnaire on / off × --runs N` の matrix を直列実行
  LLM レート制限を避けるため並列化はしない (UI 同等の `_build_topology_log_text`
  → `run_rally_async` を経由)
- 出力:
  - 標準出力: Markdown 比較表 + on/off 平均集計
  - `--output <path>`: CSV 出力 (`label / questionnaire / confidence /
    rounds / tokens_in / tokens_out / latency_ms_total / elapsed_wall_s /
    top_category / top_summary`)

### 使い方
```powershell
cd apps\agents
.\.venv\Scripts\Activate.ps1
python scripts\benchmark_questionnaire.py `
    --scenario ..\..\samples\topology\scenario2_api_acl_missing `
    --runs 1 --output bench-scenario2.csv
```

### 評価軸
- **精度**: `suspected_node_ids` の意図されたノードへの一致 / `top_category` / `top_summary`
- **速度**: `latency_ms_total` / `elapsed_wall_s` / `delegation_rounds`
- **コスト**: `tokens_in` / `tokens_out` (LLM 単価は環境依存のためスクリプト側で計算しない)

### 既知の制約
- 並列実行なし (LLM レート制限を保守的に避けるため)
- 同シナリオを `--runs > 1` で繰り返しても LLM の確率的揺らぎが平均化される程度
  本格的な統計検定をしたい場合はさらにシナリオを増やすのが筋
- skip_config_stage や audit_after_integrator のオン/オフ matrix は未実装
  (Phase A.5 / Phase C を benchmark に組み合わせる場合は CLI 引数追加が必要)

---

## 10. 既知の留意事項

- **uvicorn のリロード**: コード変更後手動再起動（既知の罠）
- **prompt caching への影響**: Stage 2 の log_text 先頭に可変の仮説ブロックが入るので、その部分はキャッシュヒットしない。Stage 1 と Stage 2 は別 system prompt として扱われる
- **Anthropic API コスト**: 1 回の解析で 2 回 rally するため、トークン消費は単一モードの約 2 倍。検証目的では許容範囲
- **abort 時の表示**: 最終結果 Tab は「Stage 1 のみ」を表示、「統合」「Stage 2」は無効化
- **ブラウザ閉じた時の挙動**: SSE が切れると Stage 中断扱い。状態復元は MVP では未対応（リトライ前提）
