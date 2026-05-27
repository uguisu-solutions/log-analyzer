# scenario2_api_acl_missing — Terraform 定義

scenario2 (FW ACL コメントアウト漏れによる API サーバ片肺) を **Terraform で
表現したインフラ定義** 一式。`Terraform 一括取込` ボタンの動作確認用。

## ファイル構成

| ファイル | 内容 |
|---|---|
| [main.tf](./main.tf) | provider / terraform backend 設定 |
| [variables.tf](./variables.tf) | region / VPC / AMI / インスタンスタイプ 等の変数定義 |
| [network.tf](./network.tf) | VPC / Subnet / IGW / Route Table |
| [security.tf](./security.tf) | **障害源**: fw-01 / lb-01 の Security Group。`api_01` 向け ingress がコメントアウト |
| [compute.tf](./compute.tf) | EC2: web-01 / api-01 |
| [lb.tf](./lb.tf) | 内部 ALB lb-01 + Target Group + Listener |
| [outputs.tf](./outputs.tf) | LB DNS / SG ID / instance ID |

## ノード ID とリソース label の対応

`Terraform 一括取込` モーダルが自動マッチするため、以下の規約で命名:

| Terraform リソース | ノード id | マッチ根拠 |
|---|---|---|
| `aws_security_group.fw_01` | `fw-01` | label の `_` ↔ `-` 正規化 + `tags.Name = "fw-01"` |
| `aws_security_group.lb_01` | `lb-01` | 同上 |
| `aws_instance.web_01` | `web-01` | label / tags.Name |
| `aws_instance.api_01` | `api-01` | label / tags.Name |

`aws_lb.lb_01` / `aws_subnet.*` / `aws_vpc.main` 等もマッチ候補に挙がるが、
ノード id と一致するのは上記 4 件のみ。

## UI からの取り込み手順

1. トポロジー解析タブ or Config-First 解析タブを開く
2. 構成図画像をアップロード → 4 ノードの矩形を描画 (id を `fw-01` / `lb-01` /
   `web-01` / `api-01` に設定)
3. ツールバー「**Terraform 一括取込**」ボタンをクリック
4. モーダルで `security.tf` (または下記の bundled) をファイル選択 or 貼り付け
5. 「解析してプレビュー」→ マッチ結果を確認
6. 「N 件を各ノードに割当」で適用

## bundled.tf を作りたい場合

UI は 1 回 1 ファイル取込なので、複数ファイルを一度に取り込みたいなら
連結したものを作る:

```powershell
Get-Content *.tf | Set-Content bundled.tf
```

または PowerShell 以外:

```bash
cat main.tf variables.tf network.tf security.tf compute.tf lb.tf outputs.tf > bundled.tf
```

## scenario1 (lb_fw_denial) との違い

scenario1 は LB-FW の `policy reload で `lb-to-app-01` が抜けた」ASA-style ACL
が主題でしたが、こちらは **Terraform 管理下の AWS Security Group コメントアウト
漏れ** が主題。同じ障害パターンを別レイヤ (IaC レイヤ) で再現しているので、
「コンフィグを Terraform で表現した場合に LLM が同じ結論にたどり着けるか」
という検証ができます。
