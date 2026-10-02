// Claude Code の PostToolUse フック（.claude/settings.json から呼ばれる）。
// Edit / Write したファイルだけを lint にかけ、エラーがあれば exit 2 で Claude に差し戻す。
//
//   .py                  → ruff（設定は apps/agents/pyproject.toml の [tool.ruff]）
//   apps/ui の ts/tsx 等 → eslint（設定は apps/ui/eslint.config.js）
//   その他の .mjs / .js  → node --check（ルートには eslint 設定を置いていないため構文のみ）
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { relative, sep } from "node:path";

const input = JSON.parse(readFileSync(0, "utf8"));
const filePath = input?.tool_input?.file_path ?? "";
if (!filePath) process.exit(0);

const rel = relative(process.cwd(), filePath).split(sep).join("/");
// リポジトリ外・依存・生成物・仮想環境は見ない
if (!rel || rel.startsWith("..")) process.exit(0);
if (/(?:^|\/)(?:node_modules|\.venv|dist|\.git|__pycache__|\.pytest_cache|\.ruff_cache)(?:\/|$)/.test(rel)) {
  process.exit(0);
}

const fail = (label, out) => {
  console.error(`${label}: ${rel}\n${out}`.trim());
  process.exit(2);
};

/** apps/agents の仮想環境の python を探す。CI など venv が無い環境では python にフォールバックする。 */
const python = () =>
  [
    process.env.LOG_ANALYZER_PYTHON,
    "apps/agents/.venv/Scripts/python.exe",
    "apps/agents/.venv/bin/python",
  ].filter(Boolean).find((p) => existsSync(p)) ?? "python";

if (rel.endsWith(".py")) {
  const r = spawnSync(python(), ["-m", "ruff", "check", "--output-format=concise", rel], {
    encoding: "utf8",
  });
  if (r.status !== 0) fail("ruff", `${r.stdout}${r.stderr}`);
  process.exit(0);
}

if (/^apps\/ui\/.+\.(?:ts|tsx|js|jsx|mjs)$/.test(rel)) {
  if (!existsSync("apps/ui/node_modules")) process.exit(0); // 依存未インストールなら黙って通す
  const r = spawnSync("npx", ["eslint", "--no-warn-ignored", rel.replace(/^apps\/ui\//, "")], {
    cwd: "apps/ui",
    encoding: "utf8",
    shell: true,
  });
  if (r.status !== 0) fail("eslint", `${r.stdout}${r.stderr}`);
  process.exit(0);
}

if (/\.(?:mjs|js)$/.test(rel)) {
  const r = spawnSync(process.execPath, ["--check", rel], { encoding: "utf8" });
  if (r.status !== 0) fail("node --check", `${r.stdout}${r.stderr}`);
}
