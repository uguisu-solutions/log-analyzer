# SLM 検証（Phase 5）手順書

設計の正は [docs/plan/slm_support.md](plan/slm_support.md)（§7 コンテキスト長 / §9-6 精度差の観測 / §10 Phase 5）。
目的は **クラウド（Opus 4.7）と SLM（Gemma 4 26B / 31B）の最終出力の精度差を観測すること**。推論方法は同一で、違うのはモデルだけ。

| 項目 | 値 |
|---|---|
| シナリオ | `samples/topology/scenario1_lb_fw_denial`（FW ポリシ反映漏れで LB のヘルスチェック失敗）と `samples/topology/scenario2_api_acl_missing`（FW の ACL コメントアウト漏れで API が片肺）。正解は各フォルダの README。2 本にしたのは、scenario2 は手元の e4b（8B 級）でも正解したため、1 本だけでは差が出ない恐れがあるから（2026-10-16） |
| 構成 | 構成4（委譲チェーン、7 ノード）。ラウンド上限 3（`compare_slm.py` の既定） |
| 比べるもの | `config4`（クラウド）／ 26B 版のユーザー定義構成 ／ 31B 版のユーザー定義構成 |
| 回数 | 各 **3 回** |
| Pod | Runpod A40（48GB）。**起動は 1 回だけ**（課金は起動時間に比例） |

**課金が発生するもの**（すべて人が実行する）：`compare_slm.py`（クラウド分は API 課金、SLM 分は Pod の時間）、
`evaluation_agent`（Opus）、Pod の起動。Runpod に送ってよいのは合成ログ（`samples/`）だけ。

全体の流れ：

```
[Pod 前]  1 起動確認 → 2 SLM 版の構成を保存 → 3 手元 e4b でリハーサル → 4 クラウドを 3 回
[Pod 中]  5 Pod 起動・トンネル → 6 num_ctx 実測 → 7 26B を 3 回 → 8 31B を 3 回 → 9 Pod 停止
[Pod 後]  10 CSV と手採点 → 11 審判（任意） → 12 結果を貼る
```

コマンドはすべて **Windows PowerShell 5.1** で動く形。特に断りがなければ作業ディレクトリは
`C:\Users\develop\Desktop\prottype1\apps\agents`、仮想環境は有効化済み（`.\.venv\Scripts\Activate.ps1`）とする。

---

## 1. バックエンドの起動確認（Pod 前）

**目的**：以降の手順が使うバックエンドが動いていることを確かめる。

1. 前提：`apps/agents/.env` に Anthropic のキーが入っている。Langfuse は無くてもよい（無いとエラー表示と 8 秒待ちが出るだけ）
2. 実行（ターミナル A。以降このターミナルはバックエンド専用）：
   ```powershell
   cd C:\Users\develop\Desktop\prottype1\apps\agents
   .\.venv\Scripts\Activate.ps1
   uvicorn log_analyzer.api:app --port 8000
   ```
3. 確認（ターミナル B）：
   ```powershell
   Invoke-RestMethod http://localhost:8000/api/inference-models | ConvertTo-Json -Depth 5
   ```
4. 期待結果：JSON が返り、SLM の接続先として `http://127.0.0.1:11434`（手元の Ollama）が表示される
5. 失敗の見分け方：`接続できません` → ターミナル A のエラーを見る。ポート衝突なら `--port 8001` にして以降の URL も合わせる
6. 貼り返すもの：なし（失敗したらターミナル A の末尾 20 行）

## 2. SLM 版のユーザー定義構成を保存する（Pod 前・1 回だけ）

**目的**：`compare_slm.py --configs user:N` で流せるよう、構成4 の 7 ノード全部を同じ SLM にした構成を 3 つ作る
（26B / 31B が本番、e4b は手順 3 のリハーサル用）。プロンプトは既定のまま（上書きなし）。

1. 前提：手順 1 のバックエンドが起動中
2. 実行（ターミナル B）：
   ```powershell
   $slots = 'orchestrator','fw_monitor','routing_monitor','app_monitor','dns_monitor','sec_monitor','integrator'
   function Save-SlmConfig($name, $model) {
     $mo = @{}; foreach ($s in $slots) { $mo[$s] = $model }
     $body = @{ name = $name; base_config = 'config4'; overrides = @{}; model_overrides = $mo } | ConvertTo-Json
     $r = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/configs/saved -ContentType 'application/json' -Body $body
     "{0} -> user:{1}" -f $r.name, $r.id
   }
   Save-SlmConfig 'slm-gemma4-26b-all' 'ollama:gemma4:26b-a4b-it-q4_K_M'
   Save-SlmConfig 'slm-gemma4-31b-all' 'ollama:gemma4:31b-it-q4_K_M'
   Save-SlmConfig 'slm-gemma4-e4b-all' 'ollama:gemma4:e4b'
   ```
3. 期待結果：`slm-gemma4-26b-all -> user:12` のような行が 3 行出る。**この 3 つの番号を控える**（以降 `user:<26B>` / `user:<31B>` / `user:<E4B>` と書く）
4. 失敗の見分け方：`400` で `model` を含むエラー → モデル名の打ち間違い（許可リストは `prompt_slots.SLM_MODELS`）。
   同じ名前で 2 回保存してしまったら、画面のユーザー定義構成一覧で古い方を消す
5. 貼り返すもの：出力の 3 行

## 3. 手元の e4b でリハーサル（Pod 前）

**目的**：Pod を起動する前に、実行 → 比較タブ → CSV までの流れを手元で一通り確かめる（Pod の時間を無駄にしない）。

1. 前提：手元の Ollama が起動し、`gemma4:e4b` がある（`curl.exe http://127.0.0.1:11434/api/tags`）。手順 1 のバックエンドが起動中
2. 計画だけ確認：
   ```powershell
   python scripts\compare_slm.py --scenario ..\..\samples\topology\scenario2_api_acl_missing --configs user:<E4B> --runs 1 --dry-run
   ```
3. 実行（SLM だけなので API 課金なし。10〜20 分ほどかかる）：
   ```powershell
   python scripts\compare_slm.py --scenario ..\..\samples\topology\scenario2_api_acl_missing --configs user:<E4B> --runs 1
   ```
4. 画面（`http://localhost:5173`）の「クラウド / SLM 比較」タブを開く
5. 期待結果：
   - スクリプトの最後に `完了 1 件 / 失敗 0 件` と出る
   - 比較タブに scenario2 の行があり、列「SLM gemma4:e4b」に 1 件入っている
   - 「CSV をダウンロード」で 1 行のファイルが取れる
6. 失敗の見分け方：
   - タイムアウト → e4b でも遅すぎる場合がある。`$env:OLLAMA_TIMEOUT_SEC = "1800"` を付けてバックエンドを起動し直す
   - CSV の `input_truncated` が 1 → 手元の `OLLAMA_NUM_CTX`（既定 32768）に入らなかった。リハーサルとしては問題ない（検知できている）
7. 貼り返すもの：スクリプトの最後の 10 行、比較タブで見えた列名

## 4. クラウドを 3 回（Pod 前・API 課金あり）

**目的**：クラウド側は Pod が要らないので先に済ませておく。

1. 前提：手順 1 のバックエンドが起動中
2. 実行：
   ```powershell
   python scripts\compare_slm.py --scenario ..\..\samples\topology\scenario1_lb_fw_denial --configs config4 --runs 3
   python scripts\compare_slm.py --scenario ..\..\samples\topology\scenario2_api_acl_missing --configs config4 --runs 3
   ```
3. 期待結果：それぞれ最後に `完了 3 件 / 失敗 0 件`。比較タブにシナリオごとの行ができ、「クラウド」列に 3 件ずつ並ぶ（scenario2 は手順 3 の e4b と同じ行）
4. 失敗の見分け方：`401` / `credit` → API キーか残高（<https://console.anthropic.com/settings/billing>）。
   別の行に並んだ → 入力の条件がずれている（シナリオのパス・ラウンド上限を手順 3 と揃える）
5. 貼り返すもの：スクリプトの最後の 10 行

---

## 5. Pod を起動して SSH トンネルを張る（ここから課金）

**目的**：Runpod 上の Ollama を手元の `localhost:11435` に見せる。

1. 前提：手順 1〜4 が終わっている。Pod の準備（起動・Ollama の起動・モデル取得）は別リポジトリの検証資料
   `slm検証/docs/runpod_gemma4_a6000_validation.md` に従う。モデルは **2 つとも最初に取得**しておく（約 38GB）：
   `gemma4:26b-a4b-it-q4_K_M` / `gemma4:31b-it-q4_K_M`
2. 実行（ターミナル C。張りっぱなしにする。Pod の 11434 番は外部に公開しない）：
   ```powershell
   ssh -N -L 11435:127.0.0.1:11434 root@<POD_IP> -p <SSH_PORT> -i ~\.ssh\<鍵>
   ```
3. 確認（ターミナル B）：
   ```powershell
   curl.exe http://localhost:11435/api/version
   curl.exe http://localhost:11435/api/tags
   ```
4. 期待結果：バージョンが返り（**0.40.5 以降**。`truncate: false` を使うため）、タグに 2 モデルが並ぶ
5. 失敗の見分け方：`Connection refused` → ターミナル C のトンネルが切れている。バージョンが古い → Pod の Ollama を更新する
   （古いと超過の検知が効かず、手順 6 の表で「検知漏れ」になる）
6. 貼り返すもの：`/api/version` の出力

## 6. num_ctx を実測して決める（Pod・1 モデル 10 分ほど）

**目的**：入力が全部入る `OLLAMA_NUM_CTX` を、VRAM に載る範囲で決める（設計 §7-3）。2 モデルで**同じ値**を使う（条件を揃えるため）。

1. 前提：手順 5 のトンネルが張れている
2. 実行（ターミナル B。モデルごとに 1 回）：
   ```powershell
   $env:OLLAMA_BASE_URL = "http://localhost:11435"
   python scripts\probe_ollama_ctx.py --model gemma4:26b-a4b-it-q4_K_M
   python scripts\probe_ollama_ctx.py --model gemma4:31b-it-q4_K_M
   ```
   既定では num_ctx を 32768 / 65536 / 98304 / 131072、入力を各 0.5 / 0.9 / 1.2 倍で試す
3. 期待結果：表の最後に `推奨 OLLAMA_NUM_CTX = <値>` が出る。各 num_ctx の 1.2 倍の行は「切り捨てを検知」になる
4. 決め方：**2 モデルの推奨値の小さい方**を使う
5. 失敗の見分け方：
   - 「検知漏れ」がある → Ollama が超過を黙って切り詰めている。**ここで止めて**表を貼る（検証の前提が崩れる）
   - 0.5 / 0.9 倍の行が「失敗」（out of memory など）→ その num_ctx は VRAM に載らない。推奨値はそれより小さい値になる
   - 「推奨できる num_ctx が無い」→ 32768 でも載らない。表を貼って相談
6. 貼り返すもの：2 モデル分の表（全行）

## 7. 26B を 3 回（Pod）

1. 前提：手順 6 で値が決まっている
2. バックエンドを Runpod 向けに起動し直す（ターミナル A を Ctrl+C で止めてから）：
   ```powershell
   $env:OLLAMA_BASE_URL = "http://localhost:11435"
   $env:OLLAMA_NUM_CTX = "<手順 6 で決めた値>"
   uvicorn log_analyzer.api:app --port 8000
   ```
3. 実行（ターミナル B）：
   ```powershell
   python scripts\compare_slm.py --scenario ..\..\samples\topology\scenario1_lb_fw_denial --configs user:<26B> --runs 3
   python scripts\compare_slm.py --scenario ..\..\samples\topology\scenario2_api_acl_missing --configs user:<26B> --runs 3
   ```
4. 期待結果：それぞれ最後に `完了 3 件 / 失敗 0 件`。比較タブの「SLM gemma4:26b-a4b-it-q4_K_M」列に、2 行とも 3 件ずつ並ぶ（クラウドと同じ行）。モデルの入れ替えを減らすため、1 つのモデルで 2 シナリオを続けて流す
5. 失敗の見分け方：
   - タイムアウト → `OLLAMA_TIMEOUT_SEC`（既定 600 秒）を `1800` にしてバックエンドを起動し直し、失敗した回だけ `--runs 1` で足す
   - `接続先に無い` 系のエラー → トンネルが切れた（手順 5）
6. 貼り返すもの：スクリプトの最後の 10 行

## 8. 31B を 3 回（Pod）

手順 7 と同じ。実行するのは次の 2 行だけ（バックエンドの起動し直しは不要）：

```powershell
python scripts\compare_slm.py --scenario ..\..\samples\topology\scenario1_lb_fw_denial --configs user:<31B> --runs 3
python scripts\compare_slm.py --scenario ..\..\samples\topology\scenario2_api_acl_missing --configs user:<31B> --runs 3
```

期待結果：「SLM gemma4:31b-it-q4_K_M」列に、2 行とも 3 件ずつ。貼り返すもの：スクリプトの最後の 10 行。

## 9. Pod を止める

1. 手順 8 が終わったら**すぐ** Runpod の画面で Pod を Stop / Terminate する（永続ストレージ無しなので次回はモデル取得からやり直しになる）
2. ターミナル C のトンネルを Ctrl+C で閉じる
3. 確認：Runpod の課金画面で Pod が止まっている

---

## 10. CSV を書き出して手採点（Pod 後）

1. 画面「クラウド / SLM 比較」タブ →「CSV をダウンロード」（1 実行 1 行・UTF-8 BOM 付きで Excel でそのまま開ける）
2. 正解は各シナリオの `README.md`（`scenario1_lb_fw_denial` / `scenario2_api_acl_missing`）。scenario2 は「障害ストーリ（種明かし）」
3. 各行の `manual_score` に真因への到達度を記入する（採点の基準は Excel「テストケース2」と同じ考え方）
4. 合わせて見る列：`input_truncated`（1 なら入力が入りきらなかった。その実行は精度比較から分けて扱う）、
   `format_retries` / `parse_fallbacks`（出力形式を守れたか）、レイテンシ
5. 貼り返すもの：記入後の CSV（または列ごとの集計）

## 11. 審判 evaluation_agent（任意・Opus 課金あり）

1. 前提：`GET /api/answer-scenarios` に scenario1 / scenario2 に当たる解答シナリオがある
   ```powershell
   Invoke-RestMethod http://localhost:8000/api/answer-scenarios | ConvertTo-Json -Depth 3
   ```
   無ければこの手順は飛ばす（手採点が主、審判は補助。設計 §9-6）
2. 画面の解析履歴で各実行を開き、評価パネルから解答シナリオを選んで採点する（審判のモデルは全プロファイルで Opus 固定）
3. 貼り返すもの：実行ごとの点数

## 12. 結果を貼る

次をまとめて貼れば、設計書（§7-3 の num_ctx、§10 の Phase 5）に追記する：
手順 5 のバージョン、手順 6 の表 2 つと決めた値、手順 4・7・8 の最後の 10 行、手順 10 の採点（と手順 11 の点数）。
