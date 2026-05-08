# 05. 完了基準の受け入れ手順

Phase 1 を「完了した」と宣言できる条件を、再現可能な手順に落とし込む。
3項目すべてが緑になれば Phase 2 着手判定に進む。

---

## C1. Prompt が編集できる

**確認手順**

1. <http://localhost> で Dify を開く
2. `log-analyzer-config1-compare` App を開く（[02_stack_deployment.md §2.2.3](02_stack_deployment.md) で作成済）
3. **プロンプト**欄を編集：末尾に「`- summary および action の自然文は、ですます調（敬体）で記述してください。`」を追加
4. **デバッグとプレビュー**で [samples/logs/sample_firewall.log](../../../samples/logs/sample_firewall.log) を入力し送信、`recommended_actions` の自然文がですます調（〜してください 等）に切り替わることを確認
5. 画面右上の「**公開する ▾ → 復元**」で最新公開版に戻し、再度同じ入力を送信、出力が常体（〜する 等）に戻ることを確認

**合格条件**

- [ ] Prompt をエンジニア以外（PM・明星氏）でも編集できる UI 上で編集できた
- [ ] 旧バージョンに戻せる（最新公開版への復元で可）
- [ ] 出力が変わったことを Dify 上で確認できた（ですます調 → 常体の切替）

> Dify OSS 版のバージョン管理は「最新公開版への 1 段階復元」のみで、商用版のような版差分 UI は持たない。本要件「旧版に戻せる」は 1 段階復元で満たす。フル機能の Prompt Versioning は Phase 3 で LangSmith 置換時に再導入する。

---

## C2. Multi-model が横並びで比較できる

**確認手順**

1. Dify の同 App で **右上 Compare（モデル横並び）** を有効化
2. 左に `Claude Sonnet 4.5`、右に `Claude Haiku 4.5`（または `gpt-4o`、`Bedrock Nova Pro`）を選択
3. 入力欄に [samples/logs/sample_firewall.log](../../../samples/logs/sample_firewall.log) の内容を貼り付け
4. **Run** で同時実行
5. 2モデルの出力が並んで表示されることを確認

**合格条件**

- [ ] 同一プロンプト・同一入力に対する2モデルの出力が画面上に並列表示される
- [ ] レイテンシ・トークン消費が両モデルで見える
- [ ] スクリーンショットを `docs/plan/phase1/evidence/c2_multi_model.png` として保存（任意）

---

## C3. 簡単な Agent 動作の Trace が取れる

**確認手順**

```powershell
cd apps\agents
.\.venv\Scripts\Activate.ps1
log-analyze ..\..\samples\logs\sample_firewall.log
```

1. 標準出力に `AnalysisResult` の JSON が出る（`"config_id":"config1"` が含まれる）
2. <http://localhost:3000> の Langfuse UI **Tracing → Traces** を開く
3. 一番上に `config1-baseline` の trace が現れる
4. trace を開くと：
   - **Input**: `{"log_ref": "...", "log_size_bytes": ...}`
   - **Output**: `AnalysisResult` の全文
   - 子 Generation `claude-sonnet-4-5`：モデル入出力・トークン数

**合格条件**

- [ ] `pytest` がパスする（3 件の単体テスト）
- [ ] `log-analyze` が exit code 0 で終了する
- [ ] 標準出力 JSON が `schema_version: v0.1` を含む
- [ ] Langfuse UI に trace `config1-baseline` が現れる
- [ ] trace 内の Generation にトークン数が記録されている

---

## 統合受け入れチェックリスト

```
[ ] Docker Desktop が起動している
[ ] infra/langfuse のスタックが Up
[ ] Dify のスタックが Up（上流 repo から起動）
[ ] apps/agents の .env が埋まっている
[ ] pytest が3件パス
[ ] log-analyze が JSON を出力
[ ] Langfuse trace に Generation が記録
[ ] Dify 上で Prompt 編集 + 旧版に戻せる
[ ] Dify 上で Multi-model 並列実行ができる
```

すべて緑なら Phase 1 完了 → [06_handoff_to_phase2.md](06_handoff_to_phase2.md) に進む。

---

## 失敗時のリカバリ

| 失敗 | 一次対応 |
|---|---|
| Langfuse trace が出ない | `apps/agents/.env` の Public/Secret Key を確認。`LANGFUSE_HOST` が `http://localhost:3000` か |
| Anthropic 401 | `ANTHROPIC_API_KEY` が古い／反映されていない。`.env` 再読込 |
| Pydantic ValidationError（モデル出力 JSON が崩れる） | プロンプトの「JSON ONLY」指示を強化、`max_tokens` を上げる |
| Dify Multi-model 比較が出ない | プロバイダ登録不足。Anthropic 以外にもう1つ追加 |
