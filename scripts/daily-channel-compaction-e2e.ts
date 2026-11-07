// Runs the real channel store and replay on a temporary SQLite profile. No live data or network.
// Run: bun scripts/daily-channel-compaction-e2e.ts
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { ChannelMessage } from "@openbot/contracts/ipc";
import { ChannelStore } from "../src/backend/channel-store";
import { databaseRows } from "../src/backend/database/database-rows";
import { runCauseEffect } from "../src/backend/effect-boundary";
import { OpenBotDatabase } from "../src/backend/openbot-database";

const output = resolve(import.meta.dirname, "../.openbot-build/daily-channel-compaction-e2e");
const root = await mkdtemp(join(tmpdir(), "openbot-channel-compaction-"));
const database = new OpenBotDatabase(root);
const report = { passed: false, messages: 0, captureEvents: 0, replayedChannels: 0 };

function message(channelId: string, id: string, text: string, completed = false): ChannelMessage {
  return {
    id,
    channelId,
    sequence: 0,
    author: { kind: "agent", id: "synthetic-agent", name: "Synthetic agent" },
    taskId: null,
    superseded: false,
    message: {
      id,
      author: "agent",
      text,
      createdAt: "2026-11-07T00:00:00.000Z",
      status: completed ? "completed" : "streaming",
    },
  };
}

try {
  await runCauseEffect(database.initialize());
  const store = new ChannelStore(database);
  for (const id of ["synthetic-channel", "synthetic-channel:other"]) {
    store.update(store.create(id, { name: id, title: "Synthetic", instructions: "", members: [], leadAgentId: null }));
    // An older build wrote this event through update, before capture command IDs existed.
    store.update(store.get(id), { messages: [message(id, "legacy", "Legacy content", true)] }, `legacy:${id}`);
    store.captureMessage(store.get(id), message(id, "stream", "First part"));
    store.captureMessage(store.get(id), message(id, "stream:other", "Other first part"));
  }
  for (let part = 0; part < 12; part += 1) {
    for (const id of ["synthetic-channel", "synthetic-channel:other"]) {
      store.captureMessage(store.get(id), message(id, "stream", `Content ${part}`, part === 11));
      store.captureMessage(store.get(id), message(id, "stream:other", `Other content ${part}`, part === 11));
    }
  }
  const captures = databaseRows(
    database.connection
      .prepare("SELECT command_id FROM orchestration_events WHERE command_id LIKE 'channels:capture:%'")
      .all(),
  );
  // Each of four messages retains its first event and latest event only.
  assert.equal(captures.length, 8);
  report.captureEvents = captures.length;
  const receipts = () =>
    databaseRows(
      database.connection
        .prepare(
          "SELECT command_id, last_sequence, result_json FROM orchestration_command_receipts ORDER BY command_id",
        )
        .all(),
    );
  const beforeReceipts = receipts();
  assert.equal(beforeReceipts.length, 12);
  assert.deepEqual(
    databaseRows(
      database.connection
        .prepare(
          "SELECT r.command_id FROM orchestration_command_receipts r LEFT JOIN orchestration_events e ON e.command_id = r.command_id WHERE e.command_id IS NULL",
        )
        .all(),
    ),
    [],
  );
  for (const id of ["synthetic-channel", "synthetic-channel:other"]) {
    const before = store.messages(id);
    assert.deepEqual(
      before.map((entry) => [entry.id, entry.message.text, entry.message.status]),
      [
        ["legacy", "Legacy content", "completed"],
        ["stream", "Content 11", "completed"],
        ["stream:other", "Other content 11", "completed"],
      ],
    );
    const channel = store.get(id);
    store.rebuild(id);
    assert.deepEqual(store.messages(id), before);
    assert.deepEqual(store.get(id), channel);
    report.messages += before.length;
    report.replayedChannels += 1;
  }
  assert.deepEqual(receipts(), beforeReceipts);
  assert.deepEqual(
    databaseRows(database.connection.prepare("PRAGMA integrity_check").all()).map((row) => row.integrity_check),
    ["ok"],
  );
  report.passed = true;
} finally {
  database.close();
  await mkdir(output, { recursive: true });
  await writeFile(join(output, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  await rm(root, { recursive: true, force: true });
}
process.stdout.write(`${report.passed ? "PASS" : "FAIL"}: channel compaction and replay. ${output}/report.json\n`);
