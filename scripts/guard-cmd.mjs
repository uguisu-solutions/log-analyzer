// Claude Code の PreToolUse フック（.claude/settings.json から呼ばれる）。
// 危険なコマンドを実行前に止める。exit 2 で Claude Code にブロックとして差し戻る。
//
// 設計方針:
// - 手元の gcloud / bq は本番 GCP（df-ibc-poc / 東京）にリンクされている。そのため
//   「読み取り専用の verb だけ許す allowlist」で止める。denylist ではサブコマンドが
//   増えたときに素通りする。
// - 実 LLM API を叩くスクリプトは課金が発生するため人が実行する。ただし
//   **実行する場合だけ**止める（読む・grep するのは許す）。
// - ガード自身・フック・CI の書き換え経路も塞ぐ。ただし書き込み先がそれらである
//   ときだけ止める（コマンドのどこかに記号とパスが併存しただけでは止めない）。
// - パス判定は Windows の "\" を "/" に正規化してから行う。
// - 判定は「実行されるコマンド本体」に対して行い、ヒアドキュメント本文や
//   -m "..." のメッセージ引数はテキストとして除外する
//   （コミットメッセージに "git push" と書いただけで止まるのを防ぐ）。
import { readFileSync } from "node:fs";

const input = JSON.parse(readFileSync(0, "utf8"));
const cmd = input?.tool_input?.command ?? "";

// 実行されるコマンド本体だけを残す。引用符つきのパス（cat ".env" 等）は検出したいので
// 引用符そのものは剥がさず、メッセージ本文に相当する部分だけを落とす。
const body = cmd
  .replace(/<<-?\s*(['"]?)([A-Za-z_]\w*)\1[\s\S]*?^\2\s*$/gm, " HEREDOC ")
  .replace(/-m\s*(['"])[\s\S]*?\1/g, " -m MSG ")
  .replace(/-m\s*\$\([\s\S]*?\)/g, " -m MSG ");
const norm = body.replace(/\\/g, "/"); // パス区切りを正規化した判定用

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
];
for (const [re, reason] of banned) if (re.test(body)) block(reason);

// ── 2. 実 LLM API / BigQuery を叩く（課金・非決定）スクリプトの実行 ──────────
// 読む・grep するのは許し、python で動かすときだけ止める。
// `\bpy\b` は ".py" 拡張子にもマッチしてしまうので、py ランチャはコマンド位置だけ見る。
const RUNS_PYTHON = /(?:\bpython3?(?:\.exe)?\b|(?:^|[\s;|&])py\s|\buv\s+run\b|\bpoetry\s+run\b)/;
const COST_SCRIPTS = /(?:benchmark_questionnaire|compare_configs|ingest_logs_to_bq)\.py/;
if (RUNS_PYTHON.test(norm) && COST_SCRIPTS.test(norm)) {
  block("実 LLM API / BigQuery を叩く（課金）ため人が実行する");
}
if (/\blog-analyze(?:\.exe)?\b(?![^|;]*--(?:help|version))/.test(norm)) {
  block("実 LLM API を叩く（課金）ため人が実行する");
}

// ── 3. 秘密情報をシェル経由で読む（Read ツールの deny を迂回させない）────────
const READERS = /(?:\bcat\b|\btype\b|Get-Content|\bhead\b|\btail\b|\bless\b|\bmore\b|\bstrings\b|\bgrep\b|Select-String)/;
// ソース中の "service_account" という**文字列**は対象外。鍵ファイル名だけを見る。
const SECRET_PATHS = /(?:^|[\s"'=/])\.env(?=$|[\s"'.])|sa-key\.json|(?:^|[\s"'/])secrets\/|service[-_]account[^\s"']*\.json/i;
if (READERS.test(norm) && SECRET_PATHS.test(norm)) block("秘密情報は読まない");

// ── 4. ガード・フック・CI 自身の書き換え ─────────────────────────────
// 「書き込み先がそれらである」ときだけ止める。
const P = "(?:\\.claude/|scripts/guard-|\\.githooks/|\\.github/workflows)";
const protectedWrites = [
  new RegExp(`>>?\\s*["']?[^\\s"'|;&]*${P}`), // リダイレクト先
  new RegExp(`(?:\\btee\\b|Set-Content|Add-Content|Out-File)\\s+(?:-\\S+\\s+)*["']?[^\\s"'|;&]*${P}`),
  new RegExp(`\\bsed\\b[^|;]*\\s-i[^|;]*${P}`),
  new RegExp(`(?:\\bcp\\b|\\bmv\\b|Copy-Item|Move-Item)\\s[^|;]*${P}`),
];
if (protectedWrites.some((re) => re.test(norm))) {
  block("ガード・フック・CI の変更は人の確認が必要");
}

// ── 5. gcloud: 参照系だけ許す ─────────────────────────────────
// デプロイ・IAM 変更・鍵作成は人が行う（docs/deploy-runbook.md）。
// 手順書では PowerShell から `& $gc ...` の形でも呼ばれるため両表記を見る。
const USES_GCLOUD = /(?:\bgcloud\b|\$gc\b)/;
const GCLOUD_READONLY =
  /(?:\bgcloud\b|\$gc\b)\s+(?:--version|--help|version|info|components\s+list|config\s+(?:list|get|get-value)\b|auth\s+list\b|(?:[\w-]+\s+)+(?:describe|list|get-iam-policy)\b)/;
if (USES_GCLOUD.test(body) && !GCLOUD_READONLY.test(body)) {
  block("gcloud は describe / list / config get 等の参照のみ");
}

// ── 6. bq: 参照系だけ許す ────────────────────────────────────
// アプリ側は MCP Toolbox を writeMode=blocked で使う。CLI からも書き込ませない。
const USES_BQ = /(?:^|[\s;|&(])bq\s/;
const BQ_READONLY = /\bbq\s+(?:--\S+\s+)*(?:show|ls|head|version|help)\b|\bbq\s[^|;]*--dry_run\b/;
const BQ_WRITE = /\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE)\b/i;
if (USES_BQ.test(body) && (!BQ_READONLY.test(body) || BQ_WRITE.test(body))) {
  block("bq は show / ls / head / --dry_run の参照のみ");
}
