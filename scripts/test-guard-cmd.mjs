// scripts/guard-cmd.mjs の回帰テスト。`node scripts/test-guard-cmd.mjs` で実行し、
// scripts/check.mjs からも呼ばれる。
//
// ガードは「止めるべきものを止める」だけでなく「普段の操作を止めない」ことも同じくらい
// 重要（誤検知が多いと回避策を探す習慣がつき、ガード自体が無意味になる）。そのため
// 遮断すべき例と通すべき例の**両方**を固定して、ガードを触ったときに即わかるようにする。
//
// ガード本体は人の手でしか変更できない（.claude/settings.json の deny）。
// このテストが赤くなったら、ガードを緩める前に「本当に通すべき操作か」を確認すること。
import { spawnSync } from "node:child_process";

// 既定は本体。差し替え候補を試すときは GUARD_CMD=<path> で指定する。
const GUARD = process.env.GUARD_CMD ?? "scripts/guard-cmd.mjs";

/** 止めるべきコマンド */
const MUST_BLOCK = [
  // git（リモートへ届く・履歴破壊・フック回避）
  "git push origin main",
  "git reset --hard HEAD~1",
  "git clean -fdx",
  "git commit --no-verify -m 'x'",
  // 一括削除・実行時データ
  "rm -rf samples",
  "Remove-Item -Recurse -Force samples",
  "rm apps/agents/data/results.sqlite3",
  // デプロイ
  "docker push asia-northeast1-docker.pkg.dev/df-ibc-poc/log-analyzer/app:latest",
  "gcloud run deploy log-analyzer --region asia-northeast1",
  "& $gc run deploy log-analyzer --region asia-northeast1",
  "gcloud builds submit --config cloudbuild.bq.yaml",
  "gcloud iam service-accounts keys create key.json --iam-account=x@y",
  "gcloud secrets versions add ANTHROPIC_API_KEY --data-file=-",
  // BigQuery への書き込み
  "bq rm -t df-ibc-poc:logs.events",
  "bq load --source_format=CSV df-ibc-poc:logs.events ./a.csv",
  "bq query --use_legacy_sql=false 'DELETE FROM logs.events WHERE 1=1'",
  // 秘密情報の読み出し
  "cat apps/agents/.env",
  "Get-Content apps/agents/secrets/sa-key.json",
  // ガード・フック・CI 自身の書き換え
  "echo hacked > .claude/settings.json",
  "cp /tmp/x.mjs scripts/guard-cmd.mjs",
  "sed -i 's/block/pass/' scripts/guard-cmd.mjs",
  "echo '#!/bin/sh' > .githooks/pre-commit",
  // 課金が発生するスクリプトの実行
  "python apps/agents/scripts/benchmark_questionnaire.py --scenarios all",
  "./apps/agents/.venv/Scripts/python.exe apps/agents/scripts/compare_configs.py",
  "./apps/agents/.venv/Scripts/log-analyze.exe --config config4 samples/logs/sample_firewall.log",
  // 破壊的 SQL
  "psql -c 'DROP TABLE run_history'",
];

/** 通すべきコマンド（誤検知の回帰防止） */
const MUST_PASS = [
  // 日常操作
  "git status --short",
  "git add -A",
  "git diff --cached",
  "git switch -c feature/x",
  "node scripts/check.mjs",
  "./apps/agents/.venv/Scripts/python.exe -m pytest -q",
  "./apps/agents/.venv/Scripts/python.exe -m ruff check src tests",
  "cat README.md",
  "./apps/agents/.venv/Scripts/log-analyze.exe --help",
  // クラウドの参照系は許す
  "gcloud run services describe log-analyzer --region asia-northeast1",
  "gcloud config get-value project",
  "gcloud auth list",
  "bq show df-ibc-poc:logs.events",
  "bq query --dry_run 'SELECT 1'",
  // 課金スクリプトも「読む」のは許す
  "sed -n 140,160p apps/agents/scripts/benchmark_questionnaire.py",
  "grep -n 'topology_context' apps/agents/scripts/benchmark_questionnaire.py",
  // ソース中の文字列を探すのは許す（鍵ファイル名ではない）
  "grep -rn 'service_account' apps/agents/src",
  // 記号と保護パスが無関係に併存しただけでは止めない
  "printf 'x' > /tmp/bad.py && echo '{}' | node scripts/guard-lint.mjs",
  "M=~/.claude/projects/x/memory/MEMORY.md && echo '- a' >> \"$M\"",
  // コミットメッセージ中の文字列には反応しない
  "git commit -m 'chore: git push と rm -rf をブロックするガードを追加'",
];

const isBlocked = (command) =>
  spawnSync(process.execPath, [GUARD], {
    input: JSON.stringify({ tool_input: { command } }),
    encoding: "utf8",
  }).status === 2;

const failures = [];
for (const c of MUST_BLOCK) if (!isBlocked(c)) failures.push(`止まらなかった: ${c}`);
for (const c of MUST_PASS) if (isBlocked(c)) failures.push(`誤って止まった: ${c}`);

const total = MUST_BLOCK.length + MUST_PASS.length;
if (failures.length) {
  console.error(`guard-cmd: ${failures.length} / ${total} 件が期待と違う`);
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`guard-cmd: ${total} 件すべて期待どおり（遮断 ${MUST_BLOCK.length} / 通過 ${MUST_PASS.length}）`);
