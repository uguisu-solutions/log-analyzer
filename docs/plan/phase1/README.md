# Phase 1 — GUIで触れる検証環境を最短で立ち上げる

最終更新: 2026-05-07

本フェーズは、上位計画 [implementation_plan.md §11](../implementation_plan.md) で
定義された **Phase 1（PoC 立ち上げ）** の実装ドキュメントである。

---

## ゴール

> **GUI で触れる検証環境を最短で立ち上げる**

W1〜W4 の作業範囲。エンジニアが手元で「ログを投入 → モデル比較 → trace を見る」
までを一筆で確認できる状態を作る。

---

## 完了基準（必達3項目）

| # | 基準 | 確認方法 |
|---|---|---|
| C1 | Prompt が編集できる | Dify 上で App を作り、Prompt を変更して再実行できる |
| C2 | Multi-model が横並びで比較できる | Dify の Prompt IDE で Claude / Nova / GPT を同一プロンプトで実行し、結果を並べて見られる |
| C3 | 簡単な Agent 動作の Trace が取れる | 構成1ベースラインの Python 実行が Langfuse UI に Trace として現れる |

---

## デリバラブル一覧

| 区分 | パス | 内容 |
|---|---|---|
| インフラ | [infra/langfuse/docker-compose.yml](../../../infra/langfuse/docker-compose.yml) | Langfuse v2 + Postgres |
| インフラ | [infra/README.md](../../../infra/README.md) | スタック起動手順 |
| エージェント | [apps/agents/](../../../apps/agents/) | 構成1（単純LLM）ベースライン Python |
| エージェント | [apps/agents/src/log_analyzer/schema.py](../../../apps/agents/src/log_analyzer/schema.py) | **共通出力スキーマ v0.1**（Pydantic） |
| エージェント | [apps/agents/src/log_analyzer/baseline_agent.py](../../../apps/agents/src/log_analyzer/baseline_agent.py) | 構成1 実装（Anthropic SDK + Langfuse trace） |
| サンプル | [samples/logs/sample_firewall.log](../../../samples/logs/sample_firewall.log) | 動作確認用のFW誤設定ログ |
| ドキュメント | `docs/plan/phase1/01_environment.md` | 前提環境・アカウント・APIキー |
| ドキュメント | `docs/plan/phase1/02_stack_deployment.md` | Dify + Langfuse の立ち上げ |
| ドキュメント | `docs/plan/phase1/03_baseline_agent.md` | ベースラインエージェントの実行手順 |
| ドキュメント | `docs/plan/phase1/04_common_schema.md` | 共通出力スキーマ v0.1 の仕様 |
| ドキュメント | `docs/plan/phase1/05_acceptance.md` | 完了基準C1〜C3の受け入れ手順 |
| ドキュメント | `docs/plan/phase1/06_handoff_to_phase2.md` | Phase 2 への引き渡し条件 |

---

## レイヤ採用ツール（Phase 1 構成B：軽量PoC）

| レイヤ | 採用 | 備考 |
|---|---|---|
| GUI / Visual Builder | **Dify**（community, OSS） | 上流イメージをそのまま利用 |
| Agent Framework | **Anthropic Python SDK 直叩き** | Phase 2 で LangGraph に置換 |
| AgentOps / Eval | **Langfuse v2**（MIT, OSS） | Phase 3 で LangSmith に置換可（二段構え戦略） |
| Visualization | — | Phase 2 で React Flow を追加 |
| Runtime | — | Phase 2 で Step Functions を追加 |

> 上位計画の表記「Framework: OpenAI Agents SDK or CrewAI」に対して、本実装は
> **Anthropic SDK 直叩き** を採った。理由：構成1（単純LLM）はエージェント
> オーケストレーション要素が無いため、Multi-Agent SDK は過剰。Phase 2 で構成4を
> 実装するタイミングで LangGraph に切り替え、Phase 1 のスキーマ・トレース構造は
> そのまま再利用する。Anthropic SDK は Bedrock との API 互換性が高く、本番移行も
> モデルID と認証の差し替えで済む。

---

## 進め方（W1〜W4 の動き方）

| 週 | 主担当 | 主要タスク | 終了条件 |
|---|---|---|---|
| W1 | リード / インフラ | Langfuse スタック立ち上げ / API Key 発行 / `apps/agents/` 雛形コミット | `pytest` 通過、Langfuse UI が開ける |
| W2 | サブ | 構成1 ベースライン本実装、サンプルログで E2E 確認、Langfuse trace に出る | 完了基準 **C3** 達成 |
| W3 | リード | Dify を立ち上げ、構成1 と同等プロンプトを App として登録、Multi-model 比較 | 完了基準 **C1**, **C2** 達成 |
| W4 | リード / サブ | 受け入れ確認、IBC 実ログ1件で構成1 を流し、結果と trace のスクショを残す | Phase 2 着手判定 |

---

## 読む順番

1. [01_environment.md](01_environment.md) — 前提環境とアカウント
2. [02_stack_deployment.md](02_stack_deployment.md) — Dify + Langfuse の起動
3. [03_baseline_agent.md](03_baseline_agent.md) — ベースラインエージェントの実行
4. [04_common_schema.md](04_common_schema.md) — 共通出力スキーマ v0.1
5. [05_acceptance.md](05_acceptance.md) — 完了基準の受け入れ
6. [06_handoff_to_phase2.md](06_handoff_to_phase2.md) — Phase 2 への引き渡し
