// 参照プロジェクトの `npm run check` 相当。lint → test → typecheck をまとめて流す。
// pre-commit（.githooks/pre-commit）と CI（.github/workflows/check.yml）が同じこれを呼ぶ。
//
//   node scripts/check.mjs            すべて流す
//   node scripts/check.mjs guard      ガードの回帰テストだけ
//   node scripts/check.mjs backend    Python だけ（ruff → pytest）
//   node scripts/check.mjs ui         UI だけ（tsc → eslint）
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

/** apps/agents の仮想環境の python を探す。CI など venv が無い環境では python にフォールバックする。
 *  各ステップは cwd を変えて実行するため、見つけた venv は**絶対パス**で返す
 *  （spawnSync は相対パスの実行ファイルを子プロセスの cwd 基準で解決してしまう）。 */
const python = () => {
  const found = [
    process.env.LOG_ANALYZER_PYTHON,
    "apps/agents/.venv/Scripts/python.exe",
    "apps/agents/.venv/bin/python",
  ].filter(Boolean).find((p) => existsSync(p));
  if (!found) return "python";
  return found === process.env.LOG_ANALYZER_PYTHON ? found : resolve(found);
};

const scope = process.argv[2] ?? "all";
const steps = [
  // ガードの回帰テストは速いので最初に流す（壊れていたら以降を回す意味がない）
  { group: "guard", name: "guard-cmd", bin: process.execPath, args: ["scripts/test-guard-cmd.mjs"], cwd: "." },
  { group: "backend", name: "ruff", bin: python(), args: ["-m", "ruff", "check", "src", "tests", "scripts"], cwd: "apps/agents" },
  { group: "backend", name: "pytest", bin: python(), args: ["-m", "pytest", "-q"], cwd: "apps/agents" },
  { group: "ui", name: "tsc", bin: "npx", args: ["tsc", "-b"], cwd: "apps/ui", shell: true },
  { group: "ui", name: "eslint", bin: "npx", args: ["eslint", "."], cwd: "apps/ui", shell: true },
];

const failed = [];
for (const step of steps) {
  if (scope !== "all" && scope !== step.group) continue;
  if (step.group === "ui" && !existsSync("apps/ui/node_modules")) {
    console.log(`\n=== skip ${step.name}: apps/ui/node_modules が無い（npm ci を先に実行）===`);
    continue;
  }
  console.log(`\n=== ${step.name} (${step.cwd}) ===`);
  // npx は Windows では .cmd なので shell 経由で呼ぶ。その場合は引数を分けて渡すと
  // Node が DEP0190 を出すため、コマンド全体を 1 本の文字列にして渡す。
  const r = step.shell
    ? spawnSync([step.bin, ...step.args].join(" "), { cwd: step.cwd, stdio: "inherit", shell: true })
    : spawnSync(step.bin, step.args, { cwd: step.cwd, stdio: "inherit" });
  if (r.status !== 0) failed.push(step.name);
}

if (failed.length) {
  console.error(`\ncheck failed: ${failed.join(", ")}`);
  process.exit(1);
}
console.log("\ncheck passed");
