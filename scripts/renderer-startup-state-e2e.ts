// Run: bun scripts/renderer-startup-state-e2e.ts. Uses only a disposable Electron profile.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { copyFile, mkdir, mkdtemp, rm } from "node:fs/promises";
import { join, resolve } from "node:path";
import { withoutElectronRuntimeFlags } from "./electron-spawn-env";

const root = resolve(import.meta.dirname, "..");
const out = join(root, ".openbot-build", "renderer-startup-state-e2e");
await mkdir(out, { recursive: true });
const fixture = await mkdtemp(join(out, "fixture-"));
try {
  const bundle = join(fixture, "main.mjs");
  const build = spawnSync(process.execPath, [
    "build",
    join(root, "scripts/renderer-startup-state-electron.ts"),
    "--target=node",
    "--format=esm",
    "--external=electron",
    `--outfile=${bundle}`,
  ]);
  assert.equal(build.status, 0, build.stderr.toString());
  const electron = join(
    root,
    "node_modules/electron/dist",
    readFileSync(join(root, "node_modules/electron/path.txt"), "utf8").trim(),
  );
  // Match the other disposable Electron behavior checks on the Linux CI runner.
  const flags = process.platform === "linux" ? ["--no-sandbox"] : [];
  const run = spawnSync(electron, [...flags, bundle, `--fixture-dir=${fixture}`], {
    env: withoutElectronRuntimeFlags(process.env),
    timeout: 30_000,
    encoding: "utf8",
  });
  assert.ok(existsSync(join(fixture, "report.json")), run.error?.message ?? run.stderr);
  await copyFile(join(fixture, "report.json"), join(out, "report.json"));
  assert.equal(run.status, 0, `${run.stderr}\n${readFileSync(join(out, "report.json"), "utf8")}`);
  process.stdout.write(`PASS: ${join(out, "report.json")}\n`);
} finally {
  await rm(fixture, { recursive: true, force: true });
}
