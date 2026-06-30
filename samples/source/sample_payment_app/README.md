# sample_payment_app（ソースコード解析の検証用サンプル）

ソースコード解析機能（docs/plan/source_code_analysis.md）の動作確認用の小規模コードベース。
障害解析の題材として「決済リトライがタイムアウト時に二重課金しうる」程度の含みを持たせてある。

- `app/charge.py` … 決済処理（リトライ・タイムアウト）
- `app/db.py` … DB セッション
- `web/api.ts` … フロント側 API クライアント（TS）
- `db/schema.sql` … DDL（payments / users）
- `models.py` … SQLAlchemy モデル（orders）
