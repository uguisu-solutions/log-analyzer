// Claude Code の PreToolUse フック（.claude/settings.json から呼ばれる）。
// 危険なコマンドを実行前に止める。exit 2 で Claude Code にブロックとして差し戻る。
//
// 設計方針:
// - 手元の gcloud / bq は本番 GCP（df-ibc-poc / 東京）にリンクされている。そのため
//   「読み取り専用の verb だけ許す allowlist」で止める。denylist ではサブコマンドが
//   増えたときに素通りする。
// - 実 LLM API を叩くスクリプトは課金が発生するため人が実行する。
// - ガード自身・フック・CI の書き換え経路（sed -i / リダイレクト）も塞ぐ。
// - パス判定は Windows の "\" を "/" に正規化してから行う（PowerShell と bash の両対応）。
import { readFileSync } from "node:fs";

const input = JSON.parse(readFileSync(0, "utf8"));
const cmd = input?.tool_input?.command ?? "";
const norm = cmd.replace(/\\/g, "/"); // パス区切りを正規化した判定用の文字列

const block = (reason) => {
  console.error(`blocked [${reason}]: ${cmd}`);
  process.exit(2);
};

// ── 1. 常に禁止 ───────────────────────────────────────────────
const banned = [
  // git: リモートへ届く操作・履歴の破壊・フック回避
  [/\bgit\s+push\b/, "push は人が行う"],
  [/\bgit\s+reset\s+--hard\b/, "作業ツリーを破壊する"],
  [/\bgit\s+clean\s+-[a-z]*f/, "追跡外ファイルを消す"],
  [/--no-verify(?=$|\s)/, "pre-commit（guard-commit・check）を飛ばさない"],
  [/--force(?=$|\s)/, "--force は使わない"],

  // 一括削除・実行時データの破壊
  [/\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r/i, "rm -rf"],
  [/Remove-Item\b[^|;]*-Recurse/i, "Remove-Item -Recurse"],
  [/\b(?:rm|del|Remove-Item)\b[^|;]*\.sqlite3?(?=$|[\s"'])/i, "実行履歴 DB（SQLite）を消す"],

  // デプロイ
  [/\bdocker\s+push\b/, "イメージの push は人が行う"],
  [/\bvercel\b/, "デプロイは人が行う"],

  // 破壊的 SQL
  [/\bDROP\s+(?:TABLE|DATABASE|SCHEMA)\b/i, "破壊的 SQL"],
  [/\bTRUNCATE\s+TABLE\b/i, "破壊的 SQL"],

  // 実 LLM API / BigQuery を叩く（課金・非決定）スクリプト
  [/benchmark_questionnaire\.py/, "実 LLM API を叩く（課金）ため人が実行する"],
  [/compare_configs\.py/, "実 LLM API を叩く（課金）ため人が実行する"],
  [/ingest_logs_to_bq\.py/, "BigQuery へ書き込むため人が実行する"],
  [/\blog-analyze(?:\.exe)?\b(?![^|;]*--(?:help|version))/, "実 LLM API を叩く（課金）ため人が実行する"],
];
for (const [re, reason] of banned) if (re.test(cmd)) block(reason);

// ── 2. 秘密情報をシェル経由で読む（Read ツールの deny を迂回させない）────────
const READERS = /(?:\bcat\b|\btype\b|Get-Content|\bhead\b|\btail\b|\bless\b|\bmore\b|\bstrings\b|\bgrep\b|Select-String)/;
const SECRET_PATHS = /(?:\.env(?=$|[\s.'"/])|sa-key\.json|secrets\/|service[-_]account)/i;
if (READERS.test(norm) && SECRET_PATHS.test(norm)) block("秘密情報は読まない");

// ── 3. ガード・フック・CI 自身の書き換え経路 ─────────────────────────
const WRITERS = /(?:\bsed\b[^|;]*-i|>>?|\btee\b|Set-Content|Add-Content|Out-File|\bmv\b|Move-Item|\bcp\b|Copy-Item)/;
const PROTECTED_PATHS = /(?:\.claude\/|scripts\/guard-|\.githooks\/|\.github\/workflows)/;
if (WRITERS.test(norm) && PROTECTED_PATHS.test(norm)) {
  block("ガード・フック・CI の変更は人の確認が必要");
}

// ── 4. gcloud: 参照系だけ許す ─────────────────────────────────
// デプロイ・IAM 変更・鍵作成は人が行う（docs/deploy-runbook.md）。
// 手順書では PowerShell から `& $gc ...` の形でも呼ばれるため両表記を見る。
const USES_GCLOUD = /(?:\bgcloud\b|\$gc\b)/;
const GCLOUD_READONLY =
  /(?:\bgcloud\b|\$gc\b)\s+(?:--version|--help|version|info|components\s+list|config\s+(?:list|get|get-value)\b|auth\s+list\b|(?:[\w-]+\s+)+(?:describe|list|get-iam-policy)\b)/;
if (USES_GCLOUD.test(cmd) && !GCLOUD_READONLY.test(cmd)) {
  block("gcloud は describe / list / config get 等の参照のみ");
}

// ── 5. bq: 参照系だけ許す ────────────────────────────────────
// アプリ側は MCP Toolbox を writeMode=blocked で使う。CLI からも書き込ませない。
const USES_BQ = /(?:^|[\s;|&(])bq\s/;
const BQ_READONLY = /\bbq\s+(?:--\S+\s+)*(?:show|ls|head|version|help)\b|\bbq\s[^|;]*--dry_run\b/;
const BQ_WRITE = /\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE)\b/i;
if (USES_BQ.test(cmd) && (!BQ_READONLY.test(cmd) || BQ_WRITE.test(cmd))) {
  block("bq は show / ls / head / --dry_run の参照のみ");
}
