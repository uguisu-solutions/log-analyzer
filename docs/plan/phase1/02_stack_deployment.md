# 02. Dify + Langfuse スタックの立ち上げ

Phase 1 完了基準 **C1（Prompt 編集）** と **C2（Multi-model 比較）** は Dify、
**C3（Trace 可視化）** は Langfuse が担う。両者を立ち上げる手順をここに記す。

---

## 2.1 Langfuse v2 の起動（本リポジトリ同梱）

### 2.1.1 起動

```powershell
cd infra\langfuse
Copy-Item .env.example .env
# .env を編集して LANGFUSE_NEXTAUTH_SECRET / LANGFUSE_SALT を 32 バイト相当の
# ランダム文字列に置き換える。PowerShell なら以下で生成可能。
#   [System.Web.Security.Membership]::GeneratePassword(32, 0)
docker compose up -d
docker compose ps
```

正常に起動すると `langfuse-server` が `0.0.0.0:3000` をバインドする。

### 2.1.2 初回セットアップ

1. ブラウザで <http://localhost:3000> を開く
2. **Sign up** で管理者アカウントを作成（ローカル PoC なので任意のメールでよい）
3. 自動で `IBC PoC` 組織と `log-analyzer` プロジェクトが作成されている
   （`docker-compose.yml` の `LANGFUSE_INIT_*` 環境変数で初期化）
4. 左下の **Settings → API Keys → Create new API keys** を押下
5. 表示された `Public Key` (`pk-lf-...`) と `Secret Key` (`sk-lf-...`) を控える
6. `apps/agents/.env` に以下を貼る：
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx
   LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx
   LANGFUSE_HOST=http://localhost:3000
   ```

### 2.1.3 停止・リセット

```powershell
docker compose down            # 停止（データ残す）
docker compose down -v         # 完全リセット（ボリューム削除）
```

---

## 2.2 Dify の起動（上流リポジトリを使用）

Dify は同梱せず、上流の `langgenius/dify` をそのまま使う。理由：

- Dify の docker-compose は web / api / worker / nginx / sandbox / weaviate など
  10コンテナ規模で、こちらで複製・固定すると追従コストが大きい
- Phase 1 では Dify は **GUI と Multi-model 比較画面** を提供できれば十分

### 2.2.1 Clone & Up

任意のディレクトリで実行（このリポジトリの外でよい）。

```powershell
cd $env:USERPROFILE\src
git clone https://github.com/langgenius/dify.git
cd dify\docker
Copy-Item .env.example .env
docker compose up -d
docker compose ps
```

数分で起動し、<http://localhost> でセットアップ画面が開く。
管理者アカウントを作成し、ログインまで済ませる。

### 2.2.2 LLM プロバイダの登録

Dify の **Settings → Model Provider** で以下を順に登録する。

| プロバイダ | 必要 | 登録するキー |
|---|---|---|
| Anthropic | ◎ | `ANTHROPIC_API_KEY` |
| OpenAI | ○（任意・C2 のため最低1つは欲しい） | `OPENAI_API_KEY` |
| AWS Bedrock | ○（社内 AWS が使える場合） | アクセスキー / シークレット / リージョン |

> Anthropic だけで Sonnet / Haiku の 2 モデル比較は可能。完了基準 C2
> （Multi-model 比較）は満たせるが、本来の目的（Claude / Nova / GPT 横並び）には
> Bedrock または OpenAI も入れておきたい。

### 2.2.3 比較用 App の作成

Dify v1.x 系では Multi-model 比較機能が **基本 Chatbot 型** にのみ存在する（ノードベースの Chatflow / Workflow には無い）。本 App は「基本 Chatbot」で作成する。

1. **Studio → アプリを作成 → 最初から作成** を開く
2. アプリタイプ選択画面で「**初心者向けの基本的なアプリタイプ ▸**」リンクを展開し、**チャットボット**（基本 Chatbot）を選択
3. 名前: `log-analyzer-config1-compare`
4. 開いた画面の左の **プロンプト**欄に [apps/agents/src/log_analyzer/baseline_agent.py](../../../apps/agents/src/log_analyzer/baseline_agent.py) の `SYSTEM_PROMPT` をコピー
5. 画面右上のモデルセレクタで **複数モデル比較モード**（モデル名ピル付近のアイコン）を ON、または「**+ モデルを追加**」で `Claude Sonnet 4.5` と `Claude Haiku 4.5` の 2 モデルを並べる
6. 下の **Bot と話す** 欄に [samples/logs/sample_firewall.log](../../../samples/logs/sample_firewall.log) の内容を貼り付け、両モデルで結果が並ぶことを確認
7. 確認できたら画面右上の **公開する** をクリックして最新公開版として保存

これで完了基準 **C1**（Prompt 編集）と **C2**（Multi-model 比較）が満たせる。

---

## 2.3 ネットワーク構成

Phase 1 ではすべてローカル単体起動。Langfuse と Dify は別の Docker Compose で
独立に動かし、`apps/agents` の Python は両者を `localhost` 経由で叩く。

```
[apps/agents (Python)] ──HTTPS──→ Anthropic API
                       ──HTTP───→ Langfuse (localhost:3000)

[ブラウザ] ──HTTP──→ Dify (localhost)
          ──HTTP──→ Langfuse UI (localhost:3000)
```

Phase 2 で Dify と Langfuse を統合する場合（Dify から Langfuse に trace を送る）、
Dify の **Settings → Tracing** で Langfuse Public/Secret Key を登録する。
本フェーズでは必須ではない。

---

## 2.4 トラブルシュート

| 症状 | 原因 / 対処 |
|---|---|
| `langfuse-server` が起動しない | `docker compose logs langfuse-server` でエラー確認。多くは `NEXTAUTH_SECRET` 未設定 |
| `pg_isready` ループ | Postgres ボリュームが破損。`docker compose down -v` で完全リセット |
| ポート3000衝突 | 他のサービスと衝突。`docker-compose.yml` の `ports` を `3001:3000` に変更 |
| Dify の `nginx` が起動しない | Dify 側の問題。上流 README の Troubleshooting を参照 |
