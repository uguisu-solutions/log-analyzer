# 01. 前提環境とアカウント

Phase 1 を進めるために必要な事前準備を整理する。

---

## 1.1 必要なソフトウェア

| ソフトウェア | 確認コマンド | 推奨バージョン |
|---|---|---|
| Docker Desktop | `docker --version` | 24.x 以降 |
| Docker Compose | `docker compose version` | v2.20 以降 |
| Python | `python --version` | 3.11 以降（3.13で動作確認済） |
| Git | `git --version` | 任意 |

このリポジトリは **Windows 11 + Docker Desktop + Python 3.13** で検証済み。
PowerShell が既定シェル。Bash 派は WSL でも可。

## 1.2 必要なアカウント / APIキー

| 項目 | 用途 | 取得先 |
|---|---|---|
| Anthropic API Key | 構成1 ベースライン（Claude Sonnet） | <https://console.anthropic.com/> |
| Langfuse Public Key | trace 送信 | Langfuse 起動後、UI から発行 |
| Langfuse Secret Key | trace 送信 | 同上 |
| OpenAI API Key（任意） | Dify Multi-model 比較で GPT を使う場合 | <https://platform.openai.com/> |
| AWS Access Key（任意） | Dify Multi-model 比較で Bedrock 経由の Nova / Claude を使う場合 | 社内 AWS アカウント |

> Anthropic Key は **必須**。Langfuse Key は Langfuse スタックを立てた後に
> UI で発行する（[02_stack_deployment.md](02_stack_deployment.md) 参照）。
> OpenAI / AWS Key は完了基準 C2（Multi-model 比較）を満たすために
> どちらか1つ以上が必要。

## 1.3 ポート

ローカルで以下のポートが空いている必要がある。

| ポート | サービス |
|---|---|
| 3000 | Langfuse Web |
| 5432 | Langfuse Postgres（外部公開しないなら不要） |
| 80 / 443 | Dify nginx（Dify を後段で立ち上げる場合） |

`netstat -ano | findstr :3000` で衝突がないか事前確認。

## 1.4 IBC 側の準備（参考）

Phase 1 中は **サンプルログ** を使うため IBC 側依存は無い。Phase 2 で実ログを
通すタイミングまでに、以下を IBC に依頼する。

- 個人情報・機密情報のサニタイズ済みログ 3 件
- 構成図 / コンフィグの参照可否（構成2の `read_topology` ツール用）
- 10 シナリオの仮設定（明星氏作成）

詳細は上位計画 [implementation_plan.md §10](../implementation_plan.md) の W1 タスク参照。
