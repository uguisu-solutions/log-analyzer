# 06. Phase 2 への引き渡し条件

Phase 1 の完了基準（C1〜C3）が揃った時点で Phase 2（拡張）に進む。
Phase 2 では LangGraph / React Flow / Step Functions が追加され、構成3・4 が
実装される。引き渡し時点で「これは Phase 1 で固定済」と「Phase 2 で再決定」を
ここで切り分けておく。

---

## Phase 1 で固定したもの（Phase 2 でも変更しない）

| 項目 | 場所 | 備考 |
|---|---|---|
| 共通出力スキーマ v0.1 | [schema.py](../../../apps/agents/src/log_analyzer/schema.py) | 互換変更のみ可 |
| `human_judgment_required` の必須化 | スキーマ + プロンプト | 議事録 L3 対応 |
| trace 命名規則 `<config_id>-<role>` | [baseline_agent.py](../../../apps/agents/src/log_analyzer/baseline_agent.py) | 比較画面のフィルタ前提 |
| Langfuse プロジェクト構造 | `IBC PoC / log-analyzer` | Phase 3 で LangSmith に切替する場合は project_id を維持 |
| AWS 不使用方針 | [implementation_plan.md §2](../implementation_plan.md) | 2026-05-07 決定。Step Functions / Bedrock / DynamoDB / S3 を使わず Python asyncio + Anthropic & OpenAI API + SQLite + ローカル FS で代替 |
| 共通言語＝日本語 | プロンプト・出力・ドキュメント | 2026-05-07 決定。詳細は memory 参照 |

---

## Phase 2 で再決定するもの

| 項目 | 既定の方向 | 検討の余地 |
|---|---|---|
| エージェントFW | LangGraph 追加（構成4 用、構成1〜3 は素 Python のまま） | CrewAI が立ち上げ早ければサブ候補（提案v3 §7） |
| Visualization | React Flow を追加 | （代替なし） |
| Runtime | **Python `asyncio.gather`**（構成3 並列）+ **LangGraph stategraph**（構成4 ラリー） | AWS が確保できれば Step Functions に再移管（[project_no_aws](../../../) memory 参照） |
| trace 送信先 | Langfuse 継続 | 監査要件が強まれば LangSmith に切替（提案v3 二段構え戦略） |
| 結果永続化 | **SQLite**（`apps/agents/data/results.sqlite3`） | 規模が出てきたら Postgres に移行 |
| 3rd モデル（構成3） | **OpenAI GPT-4o** | Gemini など別ベンダーへの差し替え可 |
| Dify と LangGraph の役割境界 | Dify は GUI / LangGraph は実行 | Dify Workflow を実行系として使うかは Phase 2 W5 で判断 |

---

## Phase 2 着手前のリスクチェック

| リスク | 確認方法 | 担当 | 状態 |
|---|---|---|---|
| Anthropic API キー有効 | `log-analyze` が exit 0 で終了 | リード | ✅ 達成 |
| OpenAI API キー調達 | `OPENAI_API_KEY` が apps/agents/.env に設定済 | PM | 調達可確認済（2026-05-07）/ 設定は構成3 着手時に |
| フロントエンドエンジニア確保 | アサイン書 | PM | ⚠️ 未確認 |
| 構成1 baseline の精度が極端に低くない | sample_firewall.log で `confidence > 0.7` | リード | ✅ 0.95 達成 |
| 構成2 の圧縮率が機能する | sample_firewall_large.log で `compression_ratio < 0.20` | リード | ✅ 0.170 達成 |
| IBC 実ログのサニタイズ方針合意 | IBC 文書 | PM | ⚠️ 未確認 |

これらが揃わないまま Phase 2 に入ると W5（管理UI）で詰まる。

---

## Phase 2 立ち上げ時の最初の3タスク

1. **構成3 を `asyncio.gather` で実装**: Anthropic Sonnet / Anthropic Haiku / OpenAI GPT-4o の
   3並列 → 統合エージェントで `config3-multi` trace が Langfuse に出るところまで
2. **LangGraph で構成4 のスケルトンを書く**: Orchestrator + 1監視Agent（FW のみ）で
   `config4-orchestrator` trace が Langfuse に出るところまで
3. **React Flow Canvas のプロトタイプ**: ノード3つを D&D で配置、線で接続できる
   状態を作り、Dify 埋め込みの構想と比較

詳細は上位計画 [implementation_plan.md §5](../implementation_plan.md) の W5〜W8 を参照。

---

## 移管成果物（チェックリスト）

Phase 1 終了時、Phase 2 担当に手渡すもの。

- [ ] [docs/plan/phase1/](.) 一式
- [ ] [apps/agents/](../../../apps/agents/) 一式（構成1 動作確認済）
- [ ] [infra/langfuse/](../../../infra/langfuse/) 一式
- [ ] Langfuse 上に最低 5 件の `config1-baseline` trace（実行履歴）
- [ ] Dify 上に `log-analyzer-config1-compare` App が公開済で、最新公開版への
  復元動作が確認されている
- [ ] 受け入れチェックリスト（[05_acceptance.md](05_acceptance.md)）の実行記録
