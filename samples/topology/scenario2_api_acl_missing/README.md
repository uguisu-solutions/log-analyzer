# テストシナリオ 2 — FW ACL コメントアウト漏れによる API サーバ片肺

**config-log 解析タブの動作確認用**サンプル一式。
2 段階 (config → log) では Stage 1 (Configs のみ) で原因の当たりがつき、
Stage 2 (Logs) で事実確認できる設計になっている。1 段階モードや log → config 順でも
同じ素材で挙動を確かめられる。

---

## 構成

```
Internet → fw-01 (FW) → lb-01 (LB) → web-01 (Web Server) / api-01 (API Server)
```

| ノード | type | IP | 役割 |
|---|---|---|---|
| fw-01 | FW | 203.0.113.1 / 10.1.1.1 | エッジ FW + Static NAT |
| lb-01 | LB | 10.1.1.10 | HAProxy ベース、web/api を path で分岐 |
| web-01 | Server | 10.1.2.20 | nginx (Web frontend) |
| api-01 | Server | 10.1.2.21 | gunicorn (API backend) |

---

## 障害ストーリ (種明かし)

- 2026-05-25 18:30、ops02 が「インシデント調査用」と称して fw-01 の
  `access-list inside_out` の **api-backends 宛て permit ルールをコメントアウト**して
  `write memory` した。本人は「本日中に戻す」予定だったが、戻し忘れて翌日を迎えた。
- 結果として:
  - **LB → web-01 (10.1.2.20) はそのまま許可** (web-backends 用ルールは生きている)
  - **LB → api-01 (10.1.2.21) は default deny で落ちる** (api-backends 用ルールが無効)
- 翌 09:00 から、LB の TCP-check が api-01 で連続失敗、`api_pool` が backend down に。
  `/api/*` リクエストは LB から **503 NoSrv** で返るようになる。
- api-01 プロセス自体は健全 (gunicorn 起動・db pool 接続中・heartbeat 出力中) だが、
  `requests_last_60s=0` で「インバウンドが届いていない」シグナルを出している。

---

## 期待される 2 段階解析結果

### Stage 1 (Configs only)

LLM が読むべき主要観点:

- **fw-01.conf** の `access-list inside_out`:
  - `permit ... web-backends` は生きている
  - `permit ... api-backends` が `!` でコメントアウト
  - すぐ後ろに `deny ip any any log` がある → api 宛ては明示的に拒否される
  - NOTE 行に「ops02 が一時的に無効化、本日中に戻す予定」と書かれている
- **lb-01.conf** の `backend api_pool`: api-01 を backend として登録済み (LB は正常な構成)
- **web-01.conf / api-01.conf**: いずれも単独では問題なし

Stage 1 で出るべき仮説:

```json
{
  "suspected_nodes": [
    {"node_id": "fw-01", "severity": "primary",
     "summary": "inside_out ACL で api-backends 向け permit がコメントアウトされており、LB → api-01 通信が default deny される設定になっている (ops02 のメモあり)"},
    {"node_id": "api-01", "severity": "secondary",
     "summary": "LB バックエンドとして登録されているが、FW で落とされるためインバウンドが届かない可能性"}
  ]
}
```

confidence: 0.75 〜 0.85 程度 (configs だけでも構造的にはほぼ確定)

### Stage 2 (Logs verification)

ログで確認できる事実:

- **fw-01.log**:
  - 18:30:01 に ops02 が ACL 削除コマンドを実行した監査ログ
  - 09:00:05 以降、`dst=10.1.2.21/443` 宛ての deny が 5 秒間隔で連続発生
- **lb-01.log**:
  - 09:00:05 〜 15 で `api_pool/api-01` の TCP-check 3 連続失敗 → DOWN
  - `backend api_pool has no server available!` 警告
  - `/api/*` リクエストが `<NOSRV>` で 503 を返す
- **api-01.log**:
  - サービス自体は健全 (gunicorn 起動・DB プール OK)
  - **`requests_last_60s=0`** が 09:01 以降継続 → 何も届いていない
- **web-01.log**: 正常運用、ヘルスチェック OK、トラフィックも回っている

Stage 2 で仮説が裏付けられ、confidence は 0.9 以上に上がる想定。

---

## 操作手順

### 前提

uvicorn を `--reload` 付きで起動済み (コード変更が反映される状態)。

```powershell
cd c:\Users\develop\Desktop\prottype1\apps\agents
.\.venv\Scripts\Activate.ps1
uvicorn log_analyzer.api:app --port 8000 --reload
```

### UI 上の操作

1. ブラウザで http://localhost:5173 → **「config-log 解析」タブ**
2. 「画像を選択」で [diagram.svg](./diagram.svg) をアップロード
   （または diagram.svg をキャンバスに**ドラッグ＆ドロップ**）
3. 「ノード追加（ドラッグで矩形描画）」モードで、画像上の 4 ボックスに矩形を重ねて描画
4. 各矩形を選択し、サイドパネルで:
   - **id** を `fw-01` / `lb-01` / `web-01` / `api-01` に
   - **type** を `FW` / `LB` / `Server` / `Server` に
   - **ip** を `10.1.1.1` / `10.1.1.10` / `10.1.2.20` / `10.1.2.21` に
   - **設定ファイル (Config)** に対応する `<id>.conf` を**ドラッグ＆ドロップ**（または
     `＋ 追加` で貼り付け）
   - **ログファイル** は `scenario2_<id>.log` を**ドラッグ＆ドロップ**（または
     `＋ samples/logs/ から追加...` ドロップダウンから選択）
   - ※ モードで「config のみ」「log のみ」を選ぶと、不要な入力欄は自動的に隠れる
   - ※ 構成は config4 固定のためセレクタはありません
5. 解析段階で **2 段階（config → log）** を選び、実行バーで「解析を開始」
6. Stage 1 (configs) が終わると **人間承認なしで自動的に Stage 2 (logs) へ進む**
   - 期待される Stage 1 仮説は「fw-01 = primary / api-01 = secondary」
7. Stage 2 完了で結果ペインを確認
   - 「統合」「Stage 1」「Stage 2」タブで Stage 別の結果を比較できる
     （Stage 1 のコンフィグ解析結果も最終結果に保持される）
   - 構成図の `fw-01` 矩形が赤系で点滅、`api-01` が橙でハイライトされる想定

### 比較したい場合

- 1 段階モードで「config のみ」「log のみ」「config + log 同時」を順に実行し、
  精度・トークン・レイテンシを突き合わせると、各データ種別の寄与を定量比較できる。
- 2 段階の Stage 1（configs のみ）の確信度と、Stage 2（logs で検証後）の確信度を比べると
  **「ログによる裏付け」が確信度をどれだけ上げるか**を体感できる。

---

## ファイル一覧

| ファイル | 用途 |
|---|---|
| [diagram.svg](./diagram.svg) | 構成図 (800×580 SVG) |
| [fw-01.conf](./fw-01.conf) | FW config — **障害源**: api-backends 向け permit がコメントアウト |
| [fw-01.log](./fw-01.log) | ops02 の編集監査ログ + dst=10.1.2.21 への deny 連発 |
| [lb-01.conf](./lb-01.conf) | HAProxy config — api_pool / web_pool 定義 |
| [lb-01.log](./lb-01.log) | api-01 ヘルスチェック失敗 → DOWN → `<NOSRV>` 503 |
| [web-01.conf](./web-01.conf) | nginx config (正常) |
| [web-01.log](./web-01.log) | 正常運用 (web 経路は影響なし) |
| [api-01.conf](./api-01.conf) | gunicorn systemd unit + api.yaml |
| [api-01.log](./api-01.log) | プロセス健全だが `requests_last_60s=0` (沈黙) |

ログファイルは UI のドロップダウンから直接ロードできるよう、`samples/logs/scenario2_*.log`
にも同内容コピーが置かれている。

---

## 関連

- [docs/plan/config_log_stages.md](../../../docs/plan/config_log_stages.md) — config-log 解析 設計書
- [samples/topology/scenario1_lb_fw_denial/](../scenario1_lb_fw_denial/) — シナリオ 1 (トポロジー解析タブ用)
