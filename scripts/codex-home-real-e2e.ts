// Checks isolated Codex history with the real app-server. No turn or model request is started.
// `bun scripts/codex-home-real-e2e.ts`; writes .openbot-build/codex-home-real-e2e/report.json.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { access, mkdir, mkdtemp, readdir, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";
import { zstdCompressSync } from "node:zlib";
import { CodexHome } from "../src/backend/agent/codex-home";
import { CodexAppServerClient } from "../src/backend/app-server-client";
import { resolveCodexCli } from "../src/backend/cli";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { decodeRecordResponse, getRecord, getString } from "../src/backend/protocol";

const OUT = resolve(import.meta.dirname, "../.openbot-build/codex-home-real-e2e");
const root = await mkdtemp(join(tmpdir(), "openbot-codex-home-real-"));
const legacyHome = join(root, "legacy");
const home = join(root, "openbot");
const redirected = join(root, "redirected");
const ids = {
  plain: randomUUID(),
  compressed: randomUUID(),
  archived: randomUUID(),
  paginated: randomUUID(),
  child: randomUUID(),
  grandchild: randomUUID(),
  personal: randomUUID(),
};
const originals = new Map<string, Buffer>();
const question = "Synthetic employee history question";
const answer = "Synthetic employee history answer";

async function paginatedRollout() {
  const turnId = randomUUID();
  const file = join(legacyHome, "sessions", "2026", "11", "06", `rollout-2026-11-06T10-00-00-${ids.paginated}.jsonl`);
  const records = [
    {
      type: "session_meta",
      payload: {
        id: ids.paginated,
        session_id: ids.paginated,
        timestamp: "2026-11-06T10:00:00Z",
        cwd: root,
        originator: "codex_cli_rs",
        cli_version: "0.167.0",
        source: "cli",
        model_provider: "openai",
        history_mode: "paginated",
      },
    },
    {
      type: "event_msg",
      payload: {
        type: "task_started",
        turn_id: turnId,
        model_context_window: 258400,
        collaboration_mode_kind: "default",
      },
    },
    {
      type: "event_msg",
      payload: {
        type: "item_completed",
        thread_id: ids.paginated,
        turn_id: turnId,
        item: {
          type: "UserMessage",
          id: "history-user",
          content: [{ type: "text", text: question, text_elements: [] }],
        },
        completed_at_ms: 1793959200000,
      },
    },
    {
      type: "response_item",
      payload: { type: "message", role: "user", content: [{ type: "input_text", text: question }] },
    },
    {
      type: "event_msg",
      payload: {
        type: "item_completed",
        thread_id: ids.paginated,
        turn_id: turnId,
        item: {
          type: "AgentMessage",
          id: "history-assistant",
          content: [{ type: "Text", text: answer }],
          phase: "final_answer",
        },
        completed_at_ms: 1793959200000,
      },
    },
    {
      type: "response_item",
      payload: {
        type: "message",
        role: "assistant",
        content: [{ type: "output_text", text: answer }],
        phase: "final_answer",
      },
    },
    { type: "event_msg", payload: { type: "task_complete", turn_id: turnId, last_agent_message: answer } },
  ];
  const bytes = Buffer.from(
    `${records
      .map((record, ordinal) => JSON.stringify({ timestamp: "2026-11-06T10:00:00Z", ordinal, ...record }))
      .join("\n")}\n`,
  );
  await writeFile(file, bytes);
  originals.set(file, bytes);
}

async function overflowImport() {
  const source = join(root, "overflow", "legacy");
  const destination = join(root, "overflow", "openbot");
  const directory = join("sessions", "2026", "11", "06");
  const parentId = randomUUID();
  const childId = randomUUID();
  const parentFile = join(directory, `rollout-2026-11-06T10-00-00-${parentId}.jsonl`);
  const childFile = join(directory, `rollout-2026-11-06T10-00-00-${childId}.jsonl`);
  await mkdir(join(source, directory), { recursive: true });
  const parent = `${JSON.stringify({ type: "session_meta", payload: { id: parentId, source: "cli" } })}\n`;
  const payload = { id: childId, source: { subagent: { thread_spawn: { parent_thread_id: parentId, depth: 1 } } } };
  const child = `${JSON.stringify({ type: "session_meta", payload })}\n`;
  await writeFile(join(source, parentFile), parent);
  await writeFile(
    join(source, childFile),
    `${JSON.stringify({ type: "session_meta", payload: { ...payload, padding: "x".repeat(4 * 1024 * 1024) } })}\n`,
  );
  const storage = new CodexHome(destination, source);
  await assert.rejects(runCauseEffect(storage.initialize([parentId])), "Oversized child metadata must stop import");
  await assert.rejects(
    access(join(destination, ".openbot-isolated-v1")),
    "An incomplete child scan must stay retryable",
  );
  await writeFile(join(source, childFile), child);
  await runCauseEffect(storage.initialize([parentId]));
  assert.equal(await readFile(join(destination, parentFile), "utf8"), parent);
  assert.equal(await readFile(join(destination, childFile), "utf8"), child, "Retry must preserve the child session");
}

async function rollout(id: string, archived = false, compressed = false, parentId?: string) {
  const directory = archived ? join(legacyHome, "archived_sessions") : join(legacyHome, "sessions", "2026", "11", "06");
  await mkdir(directory, { recursive: true });
  const file = join(directory, `rollout-2026-11-06T10-00-00-${id}.jsonl${compressed ? ".zst" : ""}`);
  const content = `${JSON.stringify({
    timestamp: "2026-11-06T10:00:00Z",
    type: "session_meta",
    payload: {
      id,
      timestamp: "2026-11-06T10:00:00Z",
      cwd: root,
      originator: "codex_cli_rs",
      cli_version: "0.167.0",
      source: parentId ? { subagent: { thread_spawn: { parent_thread_id: parentId, depth: 1 } } } : "cli",
      model_provider: "openai",
    },
  })}\n`;
  const bytes = compressed ? zstdCompressSync(Buffer.from(content)) : Buffer.from(content);
  await writeFile(file, bytes);
  originals.set(file, bytes);
}

await mkdir(OUT, { recursive: true });
const cli = await runCauseEffect(resolveCodexCli());
const [major = 0, minor = 0] = cli.version.split(".").map(Number);
const modernHistoryApi = major > 0 || minor >= 167;
await rollout(ids.plain);
await rollout(ids.compressed, false, true);
await rollout(ids.archived, true);
await rollout(ids.child, false, true, ids.plain);
await rollout(ids.grandchild, true, false, ids.child);
await rollout(ids.personal);
await paginatedRollout();
const config = `sqlite_home = ${JSON.stringify(redirected)}\nlog_dir = ${JSON.stringify(join(redirected, "log"))}\ncli_auth_credentials_store = "keyring"\n`;
await writeFile(join(legacyHome, "config.toml"), config);
const auth = '{"OPENAI_API_KEY":"synthetic-e2e-source"}\n';
await writeFile(join(legacyHome, "auth.json"), auth);
const client = new CodexAppServerClient(cli.executable, 15_000, { CODEX_HOME: home });
let failedCase = "import-retry";
try {
  const storage = new CodexHome(home, legacyHome);
  const owned = [ids.plain, ids.compressed, ids.archived, ids.paginated];
  await mkdir(home, { recursive: true });
  await writeFile(join(home, "sessions"), "blocked destination");
  await assert.rejects(runCauseEffect(storage.initialize(owned)), "Import errors must stop startup");
  await assert.rejects(access(join(home, ".openbot-isolated-v1")), "An incomplete import must stay retryable");
  const refreshedAuth = '{"OPENAI_API_KEY":"synthetic-e2e-refreshed"}\n';
  await writeFile(join(home, "auth.json"), refreshedAuth);
  await rm(join(home, "sessions"));
  await runCauseEffect(storage.initialize(owned));
  assert.equal(
    await readFile(join(home, "auth.json"), "utf8"),
    refreshedAuth,
    "Retry must keep newer OpenBot credentials",
  );
  await rm(join(home, "auth.json"));
  await runCauseEffect(storage.initialize(owned));
  await assert.rejects(access(join(home, "auth.json")), "A completed import must not restore signed-out credentials");
  assert.equal(await readFile(join(legacyHome, "auth.json"), "utf8"), auth, "The personal login must stay unchanged");
  assert.equal(
    await readFile(join(home, "config.toml"), "utf8"),
    config,
    "Imported settings keep their original bytes",
  );
  for (const [file, bytes] of originals) {
    if (file.includes(ids.personal)) continue;
    assert.deepEqual(
      await readFile(join(home, relative(legacyHome, file))),
      bytes,
      "Imported history keeps its original bytes",
    );
  }
  if (process.platform !== "win32") {
    assert.equal((await stat(home)).mode & 0o777, 0o700, "The private home is readable only by its owner");
    assert.equal((await stat(join(home, "config.toml"))).mode & 0o777, 0o600, "Imported settings are private");
  }
  const alias = join(root, "alias");
  await symlink(legacyHome, alias, "junction");
  await assert.rejects(
    runCauseEffect(new CodexHome(alias, legacyHome).initialize(owned)),
    "A shared symlink is not isolated storage",
  );
  failedCase = "metadata-overflow-and-retry";
  await overflowImport();
  failedCase = "real-codex-resume";
  client.start();
  await runCauseEffect(
    client.request(
      "initialize",
      { clientInfo: { name: "openbot_e2e", version: "0.0.0" }, capabilities: { experimentalApi: true } },
      decodeRecordResponse,
    ),
  );
  client.notify("initialized");
  const checked = [];
  for (const id of [...owned, ids.child, ids.grandchild].filter(
    (id) => modernHistoryApi || ![ids.paginated, ids.compressed, ids.child].includes(id),
  )) {
    const read = await runCauseEffect(
      client.request("thread/read", { threadId: id, includeTurns: false }, decodeRecordResponse),
    );
    assert.equal(getString(getRecord(read, "thread"), "id"), id, "Copied history remains readable by its original ID");
    if (id === ids.archived || id === ids.grandchild) {
      await runCauseEffect(client.request("thread/unarchive", { threadId: id }, decodeRecordResponse));
    }
    const resumed = await runCauseEffect(
      client.request(
        "thread/resume",
        { threadId: id, cwd: root, approvalPolicy: "on-request", sandbox: "workspace-write" },
        decodeRecordResponse,
      ),
    );
    assert.equal(getString(getRecord(resumed, "thread"), "id"), id, "Resume keeps the original session ID");
    if (id === ids.paginated) {
      assert.equal(getString(getRecord(resumed, "thread"), "historyMode"), "paginated");
      const page = await runCauseEffect(
        client.request("thread/turns/list", { threadId: id, limit: 10 }, decodeRecordResponse),
      );
      const history = JSON.stringify(page);
      assert(history.includes(question), "Paginated user history must rebuild from the copied rollout");
      assert(history.includes(answer), "Paginated assistant history must rebuild from the copied rollout");
    }
    checked.push(id);
  }
  await assert.rejects(
    runCauseEffect(
      client.request("thread/read", { threadId: ids.personal, includeTurns: false }, decodeRecordResponse),
    ),
    "Personal Codex history must stay outside OpenBot",
  );
  for (const [file, bytes] of originals) {
    assert.deepEqual(await readFile(file), bytes, "Source history must remain unchanged");
  }
  assert(
    (await readdir(home)).some((name) => /^state_\d+\.sqlite$/u.test(name)),
    "Codex builds its index in the private home",
  );
  await assert.rejects(access(redirected), "Personal database and log overrides must not escape the private home");
  await writeFile(
    join(OUT, "report.json"),
    `${JSON.stringify({ passed: true, codexVersion: cli.version, resumed: checked.length, compressedResumed: modernHistoryApi, compressedBytesPreserved: true, archived: true, paginatedHistoryPreserved: modernHistoryApi, paginatedBytesPreserved: true, descendants: true, personalExcluded: true, sourceUnchanged: true, configurationPreserved: true, databaseIsolated: true, failureRetry: true, newerCredentialsPreserved: true, signOutPreserved: true, symlinkRejected: true, metadataOverflowRejected: true }, null, 2)}\n`,
  );
  // Machine-readable E2E result.
  process.stdout.write(`Passed: ${checked.length} original sessions resumed; personal history excluded.\n`);
} catch (failure) {
  await writeFile(
    join(OUT, "report.json"),
    `${JSON.stringify({ passed: false, codexVersion: cli.version, failedCase }, null, 2)}\n`,
  );
  throw failure;
} finally {
  await runCauseEffect(client.stop());
  await rm(root, { recursive: true, force: true });
}
