import { randomUUID } from "node:crypto";
import { createReadStream } from "node:fs";
import { chmod, link, lstat, mkdir, open, readdir, rm, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, relative, resolve, sep } from "node:path";
import { createZstdDecompress } from "node:zlib";
import { sourceText } from "@openbot/i18n/source";
import { Effect, Schema, Semaphore } from "effect";
import { writeFileAtomically } from "../atomic-json-file";
import { isMissingFileError } from "../file-errors";
import { getRecord, getString } from "../protocol";

const MARKER = ".openbot-isolated-v1";
const ROLLOUT_ID = /-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl(?:\.zst)?$/iu;
const MAX_METADATA_BYTES = 4 * 1024 * 1024;

class CodexHomeFailed extends Schema.TaggedError<CodexHomeFailed>()("CodexHomeFailed", {
  cause: Schema.Defect(),
}) {}

interface Rollout {
  id: string;
  relativePath: string;
  parentId: string | null;
}

/** Personal configuration must not redirect this account's database, logs or credentials. */
export function codexHomeArguments(home: string): string[] {
  return [
    "-c",
    `sqlite_home=${JSON.stringify(home)}`,
    "-c",
    `log_dir=${JSON.stringify(join(home, "log"))}`,
    "-c",
    'cli_auth_credentials_store="file"',
  ];
}

async function exists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch (cause) {
    if (isMissingFileError(cause)) return false;
    throw cause;
  }
}

/** Publish a private, complete copy without replacing a file that a previous attempt saved. */
async function copyExclusive(source: string, destination: string, home: string): Promise<void> {
  let directory = home;
  for (const part of relative(home, dirname(destination)).split(sep).filter(Boolean)) {
    directory = join(directory, part);
    await mkdir(directory, { recursive: true, mode: 0o700 });
    const entry = await lstat(directory);
    if (!entry.isDirectory() || entry.isSymbolicLink()) {
      throw new Error(sourceText("error.storage.unreadableFile", { path: directory }));
    }
  }
  try {
    const target = await lstat(destination);
    if (!target.isFile() || target.isSymbolicLink()) {
      throw new Error(sourceText("error.storage.unreadableFile", { path: destination }));
    }
    return;
  } catch (cause) {
    if (!isMissingFileError(cause)) throw cause;
  }
  const temporary = `${destination}.${randomUUID()}.tmp`;
  const handle = await open(temporary, "wx", 0o600);
  const input = createReadStream(source);
  try {
    await handle.writeFile(input);
    await handle.sync();
    await handle.close();
    try {
      await link(temporary, destination);
    } catch (cause) {
      if (!(cause instanceof Error && "code" in cause && cause.code === "EEXIST")) throw cause;
      const target = await lstat(destination);
      if (!target.isFile() || target.isSymbolicLink()) {
        throw new Error(sourceText("error.storage.unreadableFile", { path: destination }));
      }
    }
  } finally {
    input.destroy();
    await handle.close().catch(() => undefined);
    await rm(temporary, { force: true });
  }
}

/** Read only the metadata record, never the messages in a personal or employee transcript. */
async function parentThreadId(path: string): Promise<string | null> {
  const input = createReadStream(path);
  const stream = path.endsWith(".zst") ? input.pipe(createZstdDecompress()) : input;
  if (stream !== input) input.on("error", (cause) => stream.destroy(cause));
  const chunks: Buffer[] = [];
  let length = 0;
  try {
    for await (const chunk of stream) {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      const newline = bytes.indexOf(10);
      const record = newline === -1 ? bytes : bytes.subarray(0, newline);
      length += record.length;
      if (length > MAX_METADATA_BYTES) {
        throw new Error(sourceText("error.storage.unreadableFile", { path }));
      }
      chunks.push(record);
      if (newline !== -1) break;
    }
  } finally {
    stream.destroy();
    input.destroy();
  }
  let value: unknown;
  try {
    value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return null;
  }
  if (getString(value, "type") !== "session_meta") return null;
  const payload = getRecord(value, "payload");
  const source = getRecord(payload, "source");
  const child = getRecord(source, "subagent");
  return getString(getRecord(child, "thread_spawn"), "parent_thread_id");
}

async function rollouts(home: string, relativePath: string): Promise<Rollout[]> {
  const path = join(home, relativePath);
  if (!(await exists(path))) return [];
  const found: Rollout[] = [];
  for (const entry of await readdir(path, { withFileTypes: true })) {
    const relative = join(relativePath, entry.name);
    if (entry.isDirectory()) found.push(...(await rollouts(home, relative)));
    else if (entry.isFile()) {
      const id = ROLLOUT_ID.exec(entry.name)?.[1];
      if (id) found.push({ id, relativePath: relative, parentId: await parentThreadId(join(home, relative)) });
    }
  }
  return found;
}

/** Owns OpenBot's Codex state and the non-destructive import of its existing native sessions. */
export class CodexHome {
  readonly home: string;
  readonly #legacyHome: string;
  readonly #initializing = Semaphore.makeUnsafe(1);

  constructor(home: string, legacyHome = process.env.CODEX_HOME ?? join(homedir(), ".codex")) {
    this.home = home;
    this.#legacyHome = legacyHome;
  }

  readonly initialize = Effect.fn("CodexHome.initialize")(function* (this: CodexHome, sessionIds: readonly string[]) {
    yield* this.#initializing.withPermit(
      Effect.gen({ self: this }, function* () {
        const imported = yield* Effect.tryPromise({
          try: async () => {
            await mkdir(this.home, { recursive: true, mode: 0o700 });
            const directory = await lstat(this.home);
            if (!directory.isDirectory() || directory.isSymbolicLink()) {
              throw new Error(sourceText("error.storage.unreadableFile", { path: this.home }));
            }
            await chmod(this.home, 0o700);
            if (await exists(join(this.home, MARKER))) return false;
            if (resolve(this.home) === resolve(this.#legacyHome)) {
              throw new Error(sourceText("error.storage.unreadableFile", { path: this.home }));
            }
            for (const name of ["auth.json", "config.toml"]) {
              const source = join(this.#legacyHome, name);
              if (await exists(source)) await copyExclusive(source, join(this.home, name), this.home);
            }
            const files =
              sessionIds.length === 0
                ? []
                : [
                    ...(await rollouts(this.#legacyHome, "sessions")),
                    ...(await rollouts(this.#legacyHome, "archived_sessions")),
                  ];
            const owned = new Set(sessionIds);
            let added: boolean;
            do {
              added = false;
              for (const file of files) {
                if (file.parentId && owned.has(file.parentId) && !owned.has(file.id)) {
                  owned.add(file.id);
                  added = true;
                }
              }
            } while (added);
            for (const file of files) {
              if (owned.has(file.id)) {
                await copyExclusive(
                  join(this.#legacyHome, file.relativePath),
                  join(this.home, file.relativePath),
                  this.home,
                );
              }
            }
            return true;
          },
          catch: (cause) => new CodexHomeFailed({ cause }),
        });
        if (!imported) return;
        yield* writeFileAtomically(join(this.home, MARKER), "1\n").pipe(
          Effect.mapError((failure) => new CodexHomeFailed({ cause: failure.cause })),
        );
      }),
    );
  }, Effect.uninterruptible).bind(this);
}
