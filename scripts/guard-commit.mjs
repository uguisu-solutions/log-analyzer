// .githooks/pre-commit から呼ぶ。秘密情報・実行時データ・内部資料・実ログの
// 誤コミットを止める。exit 1 でコミットを中断する。
import { execFileSync } from "node:child_process";

const git = (...args) =>
  execFileSync("git", args, { encoding: "utf8", maxBuffer: 256 * 1024 * 1024 });
const fail = (message) => {
  console.error(`guard-commit: ${message}`);
  process.exit(1);
};

// ── 1. ステージ済み差分に秘密情報が無いか ──────────────────────────
//    ガードスクリプト自身はパターン定義を含むので検査対象から外す。
const diff = git("diff", "--cached", "-U0", "--", ".", ":!scripts/guard-*.mjs");
const added = diff.split("\n").filter((l) => l.startsWith("+") && !l.startsWith("+++"));
//    `(?=[...]*\d)` は「値に数字を含むこと」の要求。.env.example や手順書の
//    プレースホルダ（sk-ant-xxxxxxxx / KEY=KEY:latest）は数字を持たないため、
//    これだけで実値と書き分けられる（プレースホルダ名の列挙では漏れる）。
const secretPatterns = [
  [/sk-ant-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{16,}/, "Anthropic API キー"],
  [/\bsk-(?:proj-)?(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{24,}/, "OpenAI API キー"],
  [/\b(?:pk|sk)-lf-[0-9a-f]{8}-[0-9a-f]{4}/, "Langfuse キー"],
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----/, "秘密鍵"],
  [/"type"\s*:\s*"service_account"/, "GCP サービスアカウント鍵"],
  [
    /\b(?:ANTHROPIC_API_KEY|OPENAI_API_KEY|LANGFUSE_SECRET_KEY|LANGFUSE_PUBLIC_KEY|BASIC_AUTH_PASSWORD)\s*=\s*["']?(?=[^\s"']*\d)[^\s"']{16,}/i,
    "API キー・パスワードの実値",
  ],
];
const hit = secretPatterns.find(([re]) => added.some((line) => re.test(line)));
if (hit) fail(`secret found in staged diff (${hit[1]})`);

const staged = git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
  .split("\n")
  .filter(Boolean);

// ── 2. 秘密情報ファイルそのもの ───────────────────────────────────
const secretFiles = staged.filter(
  (f) =>
    (/(?:^|\/)\.env(?:\..+)?$/.test(f) && !f.endsWith(".env.example")) ||
    /(?:^|\/)secrets\//.test(f) ||
    /sa-key\.json$/.test(f) ||
    /service[-_]account[^/]*\.json$/i.test(f),
);
if (secretFiles.length) fail(`secret file staged: ${secretFiles.join(", ")}`);

// ── 3. 実行時データ・内部資料（.gitignore 済みだが -f での強制追加を止める）────
const notTrackedFiles = staged.filter(
  (f) =>
    /\.sqlite3?$/.test(f) ||
    /^apps\/agents\/data\//.test(f) ||
    /^docs\/(?:preset|reports)\//.test(f) ||
    /^meeting\//.test(f) ||
    f === "RESUME.md",
);
if (notTrackedFiles.length) {
  fail(`runtime data / internal doc staged: ${notTrackedFiles.join(", ")}`);
}

// ── 4. 実ログの混入 ──────────────────────────────────────────────
//    ログは顧客データになりうる。検証用サンプルは samples/logs/ にだけ置き、
//    サイズでも止める（追跡中サンプルの最大は 17KB 程度）。
const logFiles = staged.filter((f) => /\.log$/.test(f));
const strayLogs = logFiles.filter((f) => !f.startsWith("samples/logs/"));
if (strayLogs.length) {
  fail(`log file outside samples/logs/: ${strayLogs.join(", ")}`);
}
const LOG_SIZE_LIMIT = 512 * 1024;
const bigLogs = logFiles.filter((f) => Number(git("cat-file", "-s", `:${f}`).trim()) > LOG_SIZE_LIMIT);
if (bigLogs.length) {
  fail(`log file over ${LOG_SIZE_LIMIT} bytes（実ログの混入では?）: ${bigLogs.join(", ")}`);
}
