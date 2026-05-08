# infra/

Phase 1 で立ち上げる検証スタック。

| サブディレクトリ | 内容 |
|---|---|
| `langfuse/` | Langfuse v2（OSS LLMOps）の docker-compose。Trace / Eval / Cost を集約 |

Dify は規模が大きいため本リポジトリには同梱せず、上流の `langgenius/dify` をそのまま使う。
詳細は [docs/plan/phase1/02_stack_deployment.md](../docs/plan/phase1/02_stack_deployment.md)。

## クイックスタート

```powershell
# Langfuse
cd infra\langfuse
Copy-Item .env.example .env
# .env の SECRET / SALT を更新
docker compose up -d
# http://localhost:3000 を開く
```

初回起動後、Langfuse UI でアカウントを作成し、左下の **Settings → API Keys** で
Public/Secret キーを発行する。これを `apps/agents/.env` に貼る。

停止・撤去:

```powershell
docker compose down            # 停止のみ（データは残る）
docker compose down -v         # ボリュームも削除（リセット）
```
