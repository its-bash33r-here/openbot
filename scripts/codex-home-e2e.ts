// Runs the real service against a local Codex stand-in. No provider or user profile is used.
// Writes .openbot-build/codex-home-e2e/report.json.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { copyFile, mkdtemp, readdir, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { zstdCompressSync } from "node:zlib";
import { AGENT_PROVIDERS } from "@openbot/contracts/ipc";
import { Effect } from "effect";
import type { AgentBrowserHost } from "../src/backend/agent/turn-lifecycle";

const out = resolve(import.meta.dirname, "../.openbot-build/codex-home-e2e");
const root = await mkdtemp(join(tmpdir(), "openbot-codex-home-"));
const home = join(root, "home");
const userData = join(root, "user-data");
const legacyHome = join(home, ".codex");
const isolatedHome = join(userData, "provider-state", "codex");
const executable = join(root, "codex");
const logPath = join(root, "protocol.jsonl");
const auth = JSON.stringify({ auth_mode: "chatgpt", tokens: { access_token: "synthetic-only" } });
const personalConfig = 'sqlite_home = "/personal-only"\n';
mkdirSync(legacyHome, { recursive: true });
writeFileSync(join(legacyHome, "auth.json"), auth);
writeFileSync(join(legacyHome, "config.toml"), personalConfig);
process.env.HOME = home;
process.env.CODEX_HOME = legacyHome;
process.env.OPENBOT_CODEX_PATH = executable;

const { AgentService } = await import("../src/backend/agent-service");
const { AgentStore } = await import("../src/backend/agent-store");
const { ProviderAccountStore } = await import("../src/backend/agent/provider-account-store");
const { runCauseEffect } = await import("../src/backend/effect-boundary");
const { MailboxStore } = await import("../src/backend/mailbox-store");
const { OpenBotDatabase } = await import("../src/backend/openbot-database");

const fake = `#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
if (process.argv.includes("--version")) { console.log("codex-cli 0.200.0"); process.exit(0); }
const home = process.env.CODEX_HOME;
const authPath = path.join(home, "auth.json");
const log = (entry) => fs.appendFileSync(${JSON.stringify(logPath)}, JSON.stringify({ home, pid: process.pid, ...entry }) + "\\n");
log({ method: process.argv.includes("logout") ? "logout" : "spawn" });
if (process.argv.includes("logout")) { fs.rmSync(authPath, { force: true }); process.exit(0); }
fs.mkdirSync(home, { recursive: true });
const write = (value) => process.stdout.write(JSON.stringify(value) + "\\n");
const loaded = new Set();
const rollout = (id) => {
  for (const folder of ["sessions", "archived_sessions"]) {
    const base = path.join(home, folder);
    if (!fs.existsSync(base)) continue;
    const file = fs.readdirSync(base, { recursive: true }).find((file) => file.endsWith("-" + id + ".jsonl"));
    if (file) return path.join(base, file);
  }
  return null;
};
const draft = JSON.stringify({ name: "Home test", title: "Tester", description: "Tests storage.", avatarSeed: "home", avatarHue: null, sectionId: null });
const answer = (message) => {
  const params = message.params || {};
  const reply = (result) => write({ id: message.id, result });
  log({ method: message.method, threadId: params.threadId, type: params.type });
  switch (message.method) {
    case "initialize": return reply({});
    case "account/read": {
      const saved = fs.existsSync(authPath) ? JSON.parse(fs.readFileSync(authPath, "utf8")) : null;
      return reply({ account: saved ? { type: saved.auth_mode === "apiKey" ? "apiKey" : "chatgpt", email: "synthetic@example.invalid" } : null });
    }
    case "account/login/start":
      if (params.type === "apiKey") {
        fs.writeFileSync(authPath, JSON.stringify({ auth_mode: "apiKey" }));
        return reply({});
      }
      return reply(params.type === "chatgptDeviceCode"
        ? { type: "chatgptDeviceCode", loginId: "fixture-login", userCode: "SYNTHETIC", verificationUrl: "https://example.invalid/device" }
        : { type: "chatgpt", loginId: "fixture-login", authUrl: "https://example.invalid/login" });
    case "account/login/cancel": return reply({});
    case "account/rateLimits/read": return reply({ rateLimits: { limitId: "codex", primary: null, secondary: null }, rateLimitsByLimitId: null });
    case "model/list": return reply({ data: [{ model: "gpt-6-luna", displayName: "GPT-6 Luna" }] });
    case "plugin/list": return reply({ marketplaces: [] });
    case "config/read": return reply({ config: { mcp_servers: {} }, layers: [{ name: { type: "user", file: path.join(home, "config.toml"), profile: null }, version: "1", config: { mcp_servers: {} } }] });
    case "config/batchWrite": return reply({});
    case "thread/start": {
      const id = randomUUID();
      loaded.add(id);
      return reply({ thread: { id, turns: [] } });
    }
    case "thread/resume":
      if (!rollout(params.threadId)) return write({ id: message.id, error: { code: -32600, message: "no rollout found for thread id " + params.threadId } });
      loaded.add(params.threadId);
      return reply({ thread: { id: params.threadId, turns: [] } });
    case "thread/unsubscribe": return reply({ status: "unsubscribed" });
    case "thread/turns/list": return reply({ data: [], nextCursor: null });
    case "thread/items/list": return reply({ data: [], nextCursor: null });
    case "thread/read": return reply({ thread: { id: params.threadId, turns: [] } });
    case "turn/start": {
      const id = params.threadId;
      if (!loaded.has(id)) return write({ id: message.id, error: { code: -32600, message: "thread not loaded" } });
      const turnId = randomUUID();
      const folder = path.join(home, "sessions", "2026", "11", "06");
      fs.mkdirSync(folder, { recursive: true });
      const file = rollout(id) || path.join(folder, "rollout-2026-11-06T00-00-00-" + id + ".jsonl");
      fs.appendFileSync(file, JSON.stringify({ type: "session_meta", payload: { id } }) + "\\n");
      reply({ turn: { id: turnId, status: "inProgress", items: [] } });
      write({ method: "turn/started", params: { threadId: id, turn: { id: turnId } } });
      const item = { type: "agentMessage", id: "message-" + turnId, text: draft, phase: "final_answer" };
      write({ method: "item/completed", params: { threadId: id, turnId, item } });
      return write({ method: "turn/completed", params: { threadId: id, turn: { id: turnId, status: "completed" } } });
    }
    default: return write({ id: message.id, error: { code: -32601, message: "Unknown fixture method: " + message.method } });
  }
};
let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let newline;
  while ((newline = buffer.indexOf("\\n")) >= 0) {
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    if (!line) continue;
    const message = JSON.parse(line);
    if (message.id !== undefined && message.method) answer(message);
  }
});
`;
await writeFile(executable, fake, { mode: 0o755 });

const unused = () => Effect.die(new Error("Not used by this check."));
const browser: AgentBrowserHost = {
  listTabs: () => [],
  beginTakeover: unused,
  endTakeover: () => {},
  userControlTabFor: () => null,
  awaitUserControlRelease: unused,
  resolveUploadTarget: unused,
  handleDynamicTool: unused,
  onChanged: () => () => {},
  onControlChanged: () => () => {},
  onDocumentChanged: () => () => {},
  clearControls: () => {},
  endControl: () => {},
  close: () => Effect.void,
  clearAgentSession: () => Effect.void,
  shareTab: () => undefined,
  unshareThread: () => undefined,
  agentTab: () => null,
};
interface Entry {
  home: string;
  pid: number;
  method: string;
  threadId?: string;
  type?: string;
}
function protocol(): Entry[] {
  return existsSync(logPath)
    ? readFileSync(logPath, "utf8")
        .trim()
        .split("\n")
        .filter(Boolean)
        .map((line): Entry => JSON.parse(line))
    : [];
}
async function waitFor<T>(label: string, read: () => T | null | undefined | false): Promise<T> {
  const deadline = Date.now() + 15_000;
  for (;;) {
    const value = read();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${label}.`);
    await new Promise((done) => setTimeout(done, 20));
  }
}
async function startApp() {
  const database = new OpenBotDatabase(userData);
  const store = new AgentStore(userData, home, database);
  const mailbox = new MailboxStore(userData, store.sharedRoot, database);
  const accounts = new ProviderAccountStore(join(userData, "accounts.json"), join(userData, "provider-accounts"));
  await runCauseEffect(accounts.load());
  const service = new AgentService({
    store,
    mailbox,
    browser,
    providerAccounts: accounts,
    preferredProvider: "codex",
    offProviders: AGENT_PROVIDERS.filter((provider) => provider !== "codex"),
  });
  return { database, store, accounts, service };
}
type App = Awaited<ReturnType<typeof startApp>>;
async function stop(app: App) {
  await runCauseEffect(app.service.stop());
  app.database.close();
}
function completed(app: App, agentId: string) {
  const deliveries = app.service.listQueue(agentId).deliveries;
  return deliveries.length > 0 && deliveries.every((delivery) => delivery.status === "completed");
}
const report: { passed: boolean; cases: string[]; error?: string } = { passed: false, cases: [] };
let app = await startApp();
try {
  await runCauseEffect(app.service.initialize());
  await waitFor("Codex startup", () => protocol().find((entry) => entry.method === "spawn"));
  assert.equal(protocol().find((entry) => entry.method === "spawn")?.home, isolatedHome);
  report.cases.push("Default service process uses OpenBot storage.");
  await waitFor("Codex ready", () => app.service.getStatus().phase === "ready");
  assert.equal(readFileSync(join(isolatedHome, "auth.json"), "utf8"), auth);
  assert.equal(readFileSync(join(isolatedHome, "config.toml"), "utf8"), personalConfig);

  const agent = await runCauseEffect(
    app.service.createAgent({
      name: "History",
      description: "Keeps history.",
      avatarSeed: "history",
      avatarHue: null,
      initialMessage: "First message.",
    }),
  );
  await waitFor("first employee turn", () => completed(app, agent.id));
  const session = app.store.activeProviderSession(agent.id);
  assert.ok(session);
  const nativeId = session.externalSessionId;
  const filename = `rollout-2026-11-06T00-00-00-${nativeId}.jsonl`;
  const relative = join("sessions", "2026", "11", "06", filename);
  const bytes = readFileSync(join(isolatedHome, relative), "utf8");
  const profile = await runCauseEffect(app.service.generateProfile({ prompt: "Create a storage tester." }, []));
  assert.equal(profile.name, "Home test");
  report.cases.push("Employee turns and profile generation use OpenBot storage.");
  await stop(app);

  // Simulate an installed build's employee rollout in the former shared home.
  mkdirSync(join(legacyHome, "sessions", "2026", "11", "06"), { recursive: true });
  await copyFile(join(isolatedHome, relative), join(legacyHome, relative));
  await rename(isolatedHome, join(root, "before-upgrade"));
  const unrelatedId = randomUUID();
  const unrelated = join(
    legacyHome,
    "sessions",
    "2026",
    "11",
    "06",
    `rollout-2026-11-06T00-00-00-${unrelatedId}.jsonl`,
  );
  writeFileSync(unrelated, "personal synthetic history\n");
  const childId = randomUUID();
  const childRelative = join("archived_sessions", `rollout-2026-11-06T00-00-00-${childId}.jsonl.zst`);
  const childMetadata = {
    type: "session_meta",
    payload: {
      id: childId,
      source: { subagent: { thread_spawn: { parent_thread_id: nativeId, depth: 1 } } },
    },
  };
  const childBytes = zstdCompressSync(Buffer.from(`${JSON.stringify(childMetadata)}\n`));
  mkdirSync(join(legacyHome, "archived_sessions"), { recursive: true });
  writeFileSync(join(legacyHome, childRelative), childBytes);
  app = await startApp();
  const beforeResume = protocol().length;
  await runCauseEffect(app.service.initialize());
  await waitFor("restart ready", () => app.service.getStatus().phase === "ready");
  assert.equal(readFileSync(join(isolatedHome, relative), "utf8"), bytes);
  assert.equal(readFileSync(join(legacyHome, relative), "utf8"), bytes);
  assert.deepEqual(readFileSync(join(isolatedHome, childRelative)), childBytes);
  assert.deepEqual(readFileSync(join(legacyHome, childRelative)), childBytes);
  assert.equal(
    (await readdir(join(isolatedHome, "sessions", "2026", "11", "06"))).some((name) => name.includes(unrelatedId)),
    false,
  );
  await runCauseEffect(app.service.sendMessage({ agentId: agent.id, text: "Continue the same conversation." }));
  await waitFor("resumed employee turn", () => completed(app, agent.id));
  assert.equal(app.store.activeProviderSession(agent.id)?.externalSessionId, nativeId);
  assert.ok(
    protocol()
      .slice(beforeResume)
      .some((entry) => entry.method === "thread/resume" && entry.threadId === nativeId && entry.home === isolatedHome),
  );
  report.cases.push(
    "Legacy rollout and compressed child are preserved; restart resumes the original native session ID.",
  );

  const account = await runCauseEffect(app.accounts.create("codex"));
  assert.ok(account);
  const accountHome = app.accounts.home(account);
  writeFileSync(join(accountHome, "auth.json"), auth);
  const secondary = await runCauseEffect(
    app.service.createAgent(
      {
        name: "Secondary",
        description: "Uses another account.",
        avatarSeed: "secondary",
        avatarHue: null,
        initialMessage: "Secondary message.",
      },
      (created) => app.service.updateAgent({ agentId: created.id, providerAccountId: account.id }),
    ),
  );
  await waitFor("secondary employee turn", () => completed(app, secondary.id));
  const secondaryId = app.store.activeProviderSession(secondary.id)?.externalSessionId;
  assert.ok(
    protocol().some(
      (entry) => entry.method === "turn/start" && entry.threadId === secondaryId && entry.home === accountHome,
    ),
  );
  report.cases.push("Secondary accounts keep their separate existing home.");

  await runCauseEffect(app.service.signOutProvider("codex"));
  assert.equal(protocol().findLast((entry) => entry.method === "logout")?.home, isolatedHome);
  assert.equal(readFileSync(join(legacyHome, "auth.json"), "utf8"), auth);
  assert.equal(existsSync(join(isolatedHome, "auth.json")), false);
  await runCauseEffect(app.service.connectProvider("codex", async () => {}));
  assert.ok(
    protocol().some(
      (entry) => entry.method === "account/login/start" && entry.type === "chatgpt" && entry.home === isolatedHome,
    ),
  );
  await runCauseEffect(app.service.cancelProviderSignIn("codex"));
  await runCauseEffect(app.service.startProviderCodeLogin("codex"));
  assert.ok(
    protocol().some(
      (entry) =>
        entry.method === "account/login/start" && entry.type === "chatgptDeviceCode" && entry.home === isolatedHome,
    ),
  );
  await runCauseEffect(app.service.cancelProviderCodeLogin("codex"));
  await runCauseEffect(app.service.signInCodexWithApiKey("synthetic-api-key"));
  assert.ok(
    protocol().some(
      (entry) => entry.method === "account/login/start" && entry.type === "apiKey" && entry.home === isolatedHome,
    ),
  );
  await runCauseEffect(app.service.signOutProvider("codex"));
  await stop(app);
  app = await startApp();
  await runCauseEffect(app.service.initialize());
  await waitFor("signed-out startup", () => protocol().findLast((entry) => entry.method === "account/read"));
  assert.equal(existsSync(join(isolatedHome, "auth.json")), false);
  assert.equal(readFileSync(join(legacyHome, "auth.json"), "utf8"), auth);
  report.cases.push(
    "Browser, device, API-key login and logout stay isolated; restart does not restore a removed login.",
  );
  assert.equal(
    protocol().some((entry) => entry.home === legacyHome),
    false,
  );
  assert.equal(readFileSync(unrelated, "utf8"), "personal synthetic history\n");
  assert.equal(readFileSync(join(legacyHome, "config.toml"), "utf8"), personalConfig);
  report.passed = true;
} catch (error) {
  report.error = error instanceof Error ? (error.stack ?? error.message) : String(error);
} finally {
  await stop(app).catch(() => {});
  mkdirSync(out, { recursive: true });
  writeFileSync(join(out, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  await rm(root, { recursive: true, force: true });
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  if (!report.passed) process.exitCode = 1;
}
