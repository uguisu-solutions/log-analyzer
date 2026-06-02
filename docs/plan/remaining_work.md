# 未実装バックログ — 議事録要求と現状の差分

**作成日**: 2026-05-26
**ブランチ**: `feature/config-log-analysis`
**関連**:
  - [config_log_stages.md](config_log_stages.md) — Phase A〜F の設計書
  - [implementation_plan.md](implementation_plan.md) — 全体実装計画
  - 議事録 (2026-05-26 「トラブルシューティングにおける参照情報の範囲と検証方法」)

このドキュメントは「議事録 + これまでの設計プランで合意された機能のうち、
**現時点でまだ実装されていないもの**」を優先度別にまとめた **作業バックログ** です。
着手単位の見積りと簡易設計を添えてあるので、リード or PM がここから引いて
作業項目化できます。

---

## 1. 実装完了マトリクス

議事録の主要要求と、対応する実装フェーズの一覧です。✅ は完了、⚠ は部分実装、❌ は未着手。

| 議事録要求 | フェーズ | 状態 | 補足 |
|---|---|---|---|
| 解析結果は複数（ランキング形式ではなく）表示 | G | ✅ | `RootCauseCandidate.rank` 撤去、UI もグリッド表示に |
| 問診票を基にエージェントが実行 | B | ✅ | SQLite テンプレ + UI パネル + `_build_topology_log_text` 注入 |
| 途中での要望差し込みも可能 | E 拡張 | ✅ | ChatInput + 介入時 orchestrator 再選択 (config_log_stages §9.11) |
| コンフィグ利用 ON/OFF 切替 | A.5 → 刷新 | ✅ | config-log タブの「1 段階 / 2 段階 × データ種別」モード選択に発展 (旧 skip トグルは廃止) |
| config-log 2 段階プロセス | A | ✅ | Stage 1 → 必須承認モーダル → Stage 2 (config→log / log→config 両順) |
| 構成図上で機器ピン + ログ由来の判別 | A | ✅ | severity 別ハイライト (primary=赤+点滅 / secondary=橙) |
| ラウンド履歴・tokens・処理時間 per round | D | ✅ | RoundMetrics + UI バー表示 |
| 監査エージェント (GPT 想定) | C | ✅ | GPT-4o-mini で integrator 後に独立検証 |
| UI: チャット形式 | E + 拡張 | ✅ | デフォルト chat / ChatHistoryView (完了後) / LiveChatView (実行中) / 問診票をチャット内に配置 |
| 問診票有無の同一シナリオ評価 | F | ✅ | `benchmark_questionnaire.py` CLI |
| **評価軸: 精度・速度・コスト** | F | ⚠ | 精度 / 速度は OK、**コスト ($/円) 表示が未実装**（#3） |
| **ラウンドごとの人間体感評価（約3ラウンド）** | — | ❌ | 完全未着手（#2） |
| **ベンチマーク形式の総合レポート整備** | — | ❌ | CSV はあるが報告書雛形なし（#4） |
| メモリ管理 | — | 保留 | 議事録「別途時間を設けて議論する」 |
| 当社先行テスト → 顧客 UAT | — | プロセス | コードではなく運用事項 |

---

## 2. 未実装項目一覧（優先度別）

### 🔴 P0: 議事録で明記、未着手

| # | 項目 | 工数目安 | 議事録該当 |
|---|---|---|---|
| 2 | **ラウンドごとの人間体感評価** | 2 日 | 「ラウンドごとの人間の体感評価も実施希望（約3ラウンド想定）」 |
| 3 | **コスト ($/円) 見積もり表示** | 1 日 | 「評価軸: 精度・速度・コスト」 |
| 4 | **総合レポート雛形整備** | 1 日 (文書) | 「ベンチマーク形式の総合レポートを整備」 |

> ✅ 旧 #1「新タブで実行中の追加メッセージ投入」は **完了**。
> [ChatInput.tsx](../../apps/ui/src/ChatInput.tsx) で comment / log / config の
> 3 種類を実行中に送信可能。バックエンドでは [rally_agent.py](../../apps/agents/src/log_analyzer/rally_agent.py)
> が `_drain_appends` 検出時に `intervention_restart` を emit し、
> `orchestrator_select_first` を再実行する (詳細は config_log_stages.md §9.11)。

### 🟡 P1: 設計プランで触れたが見送ったもの

| # | 項目 | 工数目安 | 影響 |
|---|---|---|---|
| 5 | 問診票テンプレートの編集 UI | 1 日 | 現状は CRUD API のみ。PoC 中の項目調整が手間 |
| 6 | 構成図/トポロジー定義の SQLite 永続化 | 1〜2 日 | 現状 localStorage 1 件。複数構成図切替不可 |
| 7 | ノード矩形のドラッグ移動 / リサイズ | 1〜2 日 | 現状「削除→再描画」のみ |
| 8 | トポロジー / config-log タブからの実行履歴アクセス | 0.5 日 | run_history は記録済、UI 導線が薄い |

### 🟢 P2: 議事録で deferred と明記

| # | 項目 | 議事録該当 |
|---|---|---|
| 9 | **メモリ管理** | 「エージェント構成に影響するため、別途時間を設けて議論する」 |
| 10 | 当社先行テスト → 顧客 UAT | プロセス事項 |

### 🔵 P3: PoC スコープ外 / 後続フェーズで OK

| # | 項目 | 補足 |
|---|---|---|
| 11 | 監査エージェントのモデル選択 UI | 現状は `AUDIT_MODEL` 環境変数のみ |
| 12 | ~~チャット UI でのリアルタイム追跡~~ ✅ 完了 | [LiveChatView.tsx](../../apps/ui/src/LiveChatView.tsx) で SSE → ChatMessage 逐次変換済 |
| 13 | monitor プロンプトの per-tab 編集 | 回避策として user:N 経由で可能 |

---

## 3. 推奨優先順

**残 P0 (3 件)** を順番に潰すのが議事録への忠実度が最も高い。各項目はそれぞれ 1〜2 日で完了する。

```
#3 (コスト見積もり) → #2 (per-round 体感評価) → #4 (報告書雛形)
```

`#3` を `#2` より先に置く理由: `#2` の per-round 評価で「コストも見たい」となるとリワーク
になる。先に metrics 系を完成させる方が手戻りが少ない。

P1 はその後、現場フィードバックを見て必要なものから着手。P2/P3 は PM 判断。

> 旧 #1「新タブで実行中の追加メッセージ投入」は完了済み。詳細は §4 直下の
> 「#1 (完了)」エントリ参照。

---

## 4. 各項目の実装メモ

着手単位を見極めるための簡易設計です。実装着手時は別途タスクチケット化してください。

### #1 (完了) — 新タブで実行中の追加メッセージ投入

**完了日**: 2026-05-27
**実装**:
- `apps/ui/src/ChatInput.tsx`: comment / log / config の 3 タイプ選択 + 送信
- `POST /api/runs/{run_id}/append-log` に `source: "intervention:{type}:user"` で送信
- バックエンドは [rally_agent.py](../../apps/agents/src/log_analyzer/rally_agent.py) で
  `_drain_appends` が空でなければ次の監視を走らせず orchestrator を再呼び出し
  し、初期ノードを再選択する (詳細は config_log_stages.md §9.11)
- 新 SSE イベント `intervention_restart` を emit、`LiveChatView` が System
  メッセージとして表示

**関連変更**:
- `DelegationEventDTO.kind` に `"orchestrator_restart"` を追加
- 2026-05-14 で定めた「orchestrator は初回 1 回のみ」不変条件は
  「初回 + 各介入時」に緩和

---

### #2 — ラウンドごとの人間体感評価 (P0, 2 日)

**現状**:
- ラウンド単位の客観 metrics (tokens, latency) は Phase D で出る
- 主観評価 (5 段階 + 自由記述) の入力欄が無い

**設計**:
- 新テーブル `round_subjective_evals(run_id, round, score:int, comment:str, evaluated_at)`
  - score: 1-5
  - 同じ round に複数回評価は許可（更新 or 追記）
- 新エンドポイント:
  - `POST /api/runs/{run_id}/round-eval { round, score, comment }`
  - `GET  /api/runs/{run_id}/round-eval` 一覧返却
- UI:
  - `RoundMetricsView` の各行に「主観 ★★★☆☆ + コメント」入力を inline で追加
  - 実行履歴タブから過去 run の評価も閲覧 / 編集可能に
- `benchmark_questionnaire.py` の CSV に主観スコアを集計列として追加

**変更ファイル目安**:
- `apps/agents/src/log_analyzer/storage.py` (新テーブル + CRUD)
- `apps/agents/src/log_analyzer/api.py` (2 エンドポイント)
- `apps/ui/src/RoundMetricsView.tsx` (入力欄追加)
- `apps/ui/src/RunHistoryView.tsx` (過去 run 評価の表示)

**テスト**:
- 評価の永続化、score の境界値、同 round の更新動作

---

### #3 — コスト ($/円) 見積もり表示 (P0, 1 日)

**現状**:
- `Metrics.tokens_in / tokens_out` は記録済
- `cost_usd` フィールドはあるが 0 のまま

**設計**:
- `apps/agents/src/log_analyzer/pricing.py` 新設:
  ```python
  MODEL_PRICING_USD_PER_1M_TOKENS = {
      "claude-sonnet-4-5": {"in": 3.0, "out": 15.0},
      "claude-haiku-4-5":  {"in": 1.0, "out": 5.0},
      "claude-opus-4-7":   {"in": 15.0, "out": 75.0},
      "gpt-4o-mini":       {"in": 0.15, "out": 0.6},
      "gpt-4o":            {"in": 2.5, "out": 10.0},
  }
  def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
      ...
  ```
- 単価は環境変数 `LLM_PRICING_OVERRIDE_JSON` で上書き可能
- 既存の `Metrics.cost_usd` を埋める (rally_agent / multi_model_agent / baseline_agent)
- `RoundMetrics` に `cost_usd_estimate: float` を追加
- UI:
  - `RoundMetricsView` の latency 列の隣に「コスト」列追加（USD・JPY 両方、為替は固定 1USD=150JPY 等を環境変数化）
  - `AuditReport` にもコスト表示
  - `benchmark_questionnaire.py` の CSV / Markdown 表に総コスト追加

**変更ファイル目安**:
- 新規 `apps/agents/src/log_analyzer/pricing.py`
- `apps/agents/src/log_analyzer/rally_agent.py` (Metrics.cost_usd を埋める)
- `apps/ui/src/RoundMetricsView.tsx`
- `apps/agents/scripts/benchmark_questionnaire.py`

**テスト**:
- 単価計算、未知モデルの fallback、為替適用

---

### #4 — 総合レポート雛形整備 (P0, 1 日 / 文書作業)

**現状**:
- Phase F の CSV はあるが、議事録の「ベンチマーク形式の総合レポート」雛形なし

**設計**:
- `docs/reports/` に以下のテンプレートを置く（中身は実評価実施時に PM が埋める）:
  - `final_report_template.md`:
    ```markdown
    # PoC 最終評価レポート (テンプレート)
    ## 1. エグゼクティブサマリ
    ## 2. 評価対象
       - 構成 1〜5 + config-log
       - シナリオ 10 件
       - 問診票あり/なし
    ## 3. 評価結果
       ### 3.1 精度 (suspected_node_ids 一致率 等)
       ### 3.2 速度 (latency / rounds)
       ### 3.3 コスト (USD / JPY)
       ### 3.4 主観評価集計 (per-round)
    ## 4. 構成別考察
    ## 5. config-log 効果
    ## 6. 監査エージェントの有用性
    ## 7. 問診票の効果
    ## 8. 環境仕様書
    ## 9. 将来構成提案
    ## 10. 推奨事項
    ## Appendix: 全データ
    ```
  - `environment_spec_template.md`: ハードウェア / ソフトウェア / API 構成
  - `future_architecture_template.md`: 本番化に向けた構成提案

**注意**:
- `docs/reports/` は `.gitignore` 対象（経営判断材料を含むため）。
  雛形だけ `.gitignore` 例外で含めるか、`docs/templates/` に分離するのが筋。

---

### #5 — 問診票テンプレート編集 UI (P1, 1 日)

**現状**:
- API CRUD 完備 (`/api/questionnaires`)
- UI 編集モーダル無し（テンプレ選択ドロップダウンのみ）

**設計**:
- `QuestionnairePanel.tsx` に「テンプレを編集」「新規テンプレ」ボタン
- モーダルで items 一覧編集 (項目追加 / 削除 / 並べ替え / type 切替)
- 編集後 `PUT /api/questionnaires/{id}` で保存
- `default` テンプレは「複製して新規作成」のみ許可（編集ボタン無効）

---

### #6 — 構成図 / トポロジー定義の SQLite 永続化 (P1, 1〜2 日)

**現状**:
- 1 件のトポロジー定義のみ localStorage (`log-analyzer.topology-v1` / `log-analyzer.config-log-topology-v1`)

**設計**:
- 新テーブル `topologies(id, name, image_data_url, nodes_json, links_json, created_at, updated_at)`
- API: GET/POST/PUT/DELETE `/api/topologies`
- UI: タブ内に「トポロジー: [default ▼] [新規] [保存] [削除]」セレクタ
- localStorage は「最後に開いた id」だけ保存（後方互換）

---

### #7 — ノード矩形のドラッグ移動 / リサイズ (P1, 1〜2 日)

**現状**:
- 描画時のみ位置決定。修正不可

**設計**:
- 選択中ノードに 4 隅のリサイズハンドル表示
- マウスドラッグで bbox 更新 → `topology.nodes[*].{x,y,w,h}` を更新
- React 標準のドラッグハンドラ + SVG 座標変換 (既存 `toNormalized` の逆方向)

---

### #8 — トポロジー / config-log タブからの実行履歴アクセス (P1, 0.5 日)

**現状**:
- run_history は `topology-run:<short>` / `config-log-run:<short>` で記録
- 閲覧は「実行履歴」タブのみ

**設計**:
- 結果ペインの上部に「過去の解析履歴を見る」リンク → 実行履歴タブにジャンプ + フィルタ自動適用
- もしくは結果セクションの末尾に「直近 5 件の同シナリオ run」を inline 表示

---

### #9 — メモリ管理 (P2, 議事録 deferred)

議事録「エージェント構成に影響するため、別途時間を設けて議論する」。
具体的な議論待ち。

検討中の論点:
- 監視同士のメモリ共有 (現状は `monitor_results` でしか共有してない)
- 委譲チェーン超えた長期記憶 (実行 1 回ごとに state はリセット、保存はしない)
- prompt cache とメモリの位置付け整理

---

### #10 — 当社先行テスト → 顧客 UAT

プロセス事項。実装ではない。

---

### #11〜#13 — 後続フェーズ

優先度低。本 PoC のスコープに入れるかは PM 判断。

---

## 5. 次セッション開始時のチェックリスト

1. PoC の現状サマリは [config_log_stages.md](config_log_stages.md) の冒頭を参照
2. 着手項目は本ドキュメントの **§3 推奨優先順** からピック
3. 各項目の「変更ファイル目安」を参照して影響範囲を把握
4. 着手前にこの doc にチケット番号 (項目 #) と着手者 / 期日を追記すると良い

---

## 6. 関連リンク

- 議事録 (2026-05-26)
- [config_log_stages.md](config_log_stages.md) — Phase A〜F の完了済機能の設計書
- [implementation_plan.md](implementation_plan.md) — PoC 全体計画
- [../reports/poc_progress_2026-05-25.md](../reports/poc_progress_2026-05-25.md) — 直近進捗（トポロジー解析タブ）
