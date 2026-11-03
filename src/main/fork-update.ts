import { type ChildProcess, execFile, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import { renameSync, writeFileSync } from "node:fs";
import { lstat, mkdtemp } from "node:fs/promises";
import { type UserInfo, userInfo } from "node:os";
import { isAbsolute, join } from "node:path";
import { promisify } from "node:util";
import { FORK_UPDATE_PHASES, forkUpdateInProgress } from "@openbot/contracts/ipc";
import type { AppTranslate } from "@openbot/i18n";
import { redactText } from "@openbot/logging";
import { Effect, Schema } from "effect";
import type { BrowserWindow, Dialog, IpcMainInvokeEvent } from "electron";
import { z } from "zod";
import { causeHelpers } from "../backend/effect-boundary";
import { isMissingFileError } from "../backend/file-errors";
import { chooseForkMerge, forkMergeResponder } from "./fork-merge";
import { readOwnedJson, verifyHostDirectory } from "./host-update-files";
import type { IdleRestart } from "./idle-restart";

const INSTALL_CONFIG = z.object({
  source: z.string().refine(isAbsolute),
  seed: z.string().regex(/^[0-9a-f]{40}$/),
  path: z.array(z.string().refine(isAbsolute)).min(1),
});
const STATUS = z.object({ phase: z.enum(FORK_UPDATE_PHASES) });
const FAILURE = z.object({ phase: z.enum(["held", "failed"]), reason: z.string().min(1).max(1200) });
const QUIT = z
  .object({ pid: z.number().int(), nonce: z.string(), bundle: z.literal("/Applications/OpenBot.app") })
  .strict();
const exec = promisify(execFile);
const WORLD = 1_019;

// This world shares the DOM with the page, but only main can read or spend its click receipt.
// A button.click(), a click on another control, and a background IPC call cannot mint one.
export const ARM_FORK_UPDATE_CLICK = `(() => {
  if (window.consumeOpenBotForkUpdateClick) return;
  let clicked = -Infinity;
  window.addEventListener("click", (event) => {
    clicked = -Infinity;
    const button = event.target instanceof Element &&
      event.target.closest('[data-tab="updates"] button#openbot-owner-fork-update');
    if (event.isTrusted && navigator.userActivation.isActive && button && !button.disabled &&
        button.checkVisibility() && document.hasFocus()) clicked = performance.now();
  }, true);
  window.consumeOpenBotForkUpdateClick = () => {
    const valid = performance.now() - clicked < 2000;
    clicked = -Infinity;
    return valid;
  };
})()`;
export const CONSUME_FORK_UPDATE_CLICK = "window.consumeOpenBotForkUpdateClick?.() === true";

class ForkUpdateFailure extends Schema.TaggedError<ForkUpdateFailure>()("ForkUpdateFailure", {
  cause: Schema.Defect(),
}) {}
const { io } = causeHelpers(ForkUpdateFailure);

interface ForkUpdateOptions {
  mainWindow: () => BrowserWindow | null;
  enabled: () => boolean;
  idleRestart: Pick<IdleRestart, "requestForkQuit" | "endForkQuit">;
  generateMergeText: (prompt: string) => Effect.Effect<string>;
  translate: AppTranslate;
  dialog: Dialog;
  childrenRunning: () => boolean;
  quit: () => void;
}

export class ForkUpdate {
  #child: ChildProcess | null = null;
  #starting = false;

  constructor(private readonly options: ForkUpdateOptions) {}

  #main(event: IpcMainInvokeEvent) {
    const win = this.options.enabled() && this.options.mainWindow();
    return win &&
      !win.isDestroyed() &&
      event.sender === win.webContents &&
      event.senderFrame === win.webContents.mainFrame
      ? win
      : null;
  }

  readonly #owner = Effect.fn("ForkUpdate.owner")(function* (this: ForkUpdate) {
    const account = userInfo();
    const folder = join(account.homedir, "Library/Application Support/OpenBot Fork Updates");
    const runner = join(folder, "update");
    const config = join(folder, "config.json");
    yield* verifyHostDirectory(folder, account.uid);
    const [dir, exe, cfg] = yield* io(() => Promise.all([lstat(folder), lstat(runner), lstat(config)]));
    if (
      dir.uid !== account.uid ||
      (dir.mode & 0o7777) !== 0o700 ||
      !exe.isFile() ||
      exe.nlink !== 1 ||
      exe.uid !== account.uid ||
      (exe.mode & 0o7777) !== 0o700 ||
      !cfg.isFile() ||
      cfg.nlink !== 1 ||
      cfg.uid !== account.uid ||
      (cfg.mode & 0o7777) !== 0o600
    )
      return null;
    yield* readOwnedJson(config, account.uid, INSTALL_CONFIG);
    return { folder, runner, account };
  });

  readonly phase = Effect.fn("ForkUpdate.phase")(
    function* (this: ForkUpdate, event: IpcMainInvokeEvent) {
      const win = this.#main(event);
      if (!win) return null;
      const owner = yield* this.#owner().pipe(Effect.catch(() => Effect.succeed(null)));
      if (!owner || !this.#main(event)) return null;
      yield* io(() =>
        win.webContents.executeJavaScriptInIsolatedWorld(WORLD, [{ code: ARM_FORK_UPDATE_CLICK }], false),
      );
      const state = yield* readOwnedJson(join(owner.folder, "status.json"), owner.account.uid, STATUS).pipe(
        Effect.catch((error) =>
          Effect.succeed({ phase: isMissingFileError(error.cause) ? ("idle" as const) : ("failed" as const) }),
        ),
      );
      if (this.#child || this.#starting) return forkUpdateInProgress(state.phase) ? state.phase : "syncing";
      // After an app restart the old runner may still own run.lock. It can also have crashed during a phase.
      if (forkUpdateInProgress(state.phase)) {
        const result = yield* io(() =>
          exec(owner.runner, ["--status"], {
            env: this.#env(owner.account),
            timeout: 3000,
            maxBuffer: 1024,
            cwd: owner.folder,
          }),
        );
        return (yield* Effect.try(() => STATUS.parse(JSON.parse(result.stdout)))).phase;
      }
      return state.phase;
    },
    Effect.catch(() => Effect.succeed("failed" as const)),
  );

  #env(account: UserInfo<string>): NodeJS.ProcessEnv {
    return {
      HOME: account.homedir,
      USER: account.username,
      LOGNAME: account.username,
      PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
    };
  }

  readonly start = Effect.fn("ForkUpdate.start")(
    function* (this: ForkUpdate, event: IpcMainInvokeEvent) {
      const win = this.#main(event);
      if (!win?.isFocused() || this.#starting || this.#child) return null;
      this.#starting = true;
      try {
        const allowed = yield* io(() =>
          win.webContents.executeJavaScriptInIsolatedWorld(WORLD, [{ code: CONSUME_FORK_UPDATE_CLICK }], false),
        );
        if (allowed !== true) return null;
        const owner = yield* this.#owner();
        if (!owner || !this.#main(event)?.isFocused()) return null;
        const run = yield* io(() => mkdtemp(join(owner.folder, "run-")));
        const nonce = randomUUID();
        if (!this.#main(event)?.isFocused()) return null;
        const child = spawn(owner.runner, [], {
          detached: true,
          stdio: "ignore",
          cwd: owner.folder,
          env: {
            ...this.#env(owner.account),
            OPENBOT_FORK_UPDATE_RUN: run,
            OPENBOT_FORK_UPDATE_MERGE: "1",
            OPENBOT_FORK_UPDATE_PID: String(process.pid),
            OPENBOT_FORK_UPDATE_NONCE: nonce,
          },
        });
        this.#child = child;
        const token = { pid: process.pid, nonce, bundle: "/Applications/OpenBot.app" };
        const serveMerge = forkMergeResponder({
          run,
          nonce,
          uid: owner.account.uid,
          active: () => this.#child === child && this.#main(event) === win && !win.isDestroyed(),
          generate: this.options.generateMergeText,
          choose: (request, reason) =>
            chooseForkMerge(win, this.options.dialog, this.options.translate, request, reason),
        });
        const watch = setInterval(() => {
          if (this.#child !== child) return;
          Effect.runFork(this.#quitRequest(run, owner.account.uid, token, child));
          Effect.runFork(serveMerge());
        }, 2000);
        watch.unref();
        child.once("close", (code) => {
          clearInterval(watch);
          if (this.#child === child) {
            this.#child = null;
            this.options.idleRestart.endForkQuit();
            if (code && this.#main(event) === win) {
              Effect.runFork(
                readOwnedJson(join(owner.folder, "status.json"), owner.account.uid, FAILURE).pipe(
                  Effect.flatMap((failure) =>
                    io(() =>
                      this.options.dialog.showMessageBox(win, {
                        type: "warning",
                        message: this.options.translate("update.fork.stopped"),
                        detail: redactText(failure.reason),
                      }),
                    ),
                  ),
                  Effect.ignore,
                ),
              );
            }
          }
        });
        yield* io(() => once(child, "spawn"));
        child.unref();
        return "syncing" as const;
      } finally {
        this.#starting = false;
      }
    },
    Effect.catch(() => Effect.succeed("failed" as const)),
  );

  readonly #quitRequest = Effect.fn("ForkUpdate.quitRequest")(
    function* (
      this: ForkUpdate,
      run: string,
      uid: number,
      token: { pid: number; nonce: string; bundle: string },
      child: ChildProcess,
    ) {
      const request = yield* readOwnedJson(join(run, "quit-request.json"), uid, QUIT);
      if (this.#child !== child || request.pid !== token.pid || request.nonce !== token.nonce) return;
      this.options.idleRestart.requestForkQuit(() => {
        if (this.#child !== child || child.exitCode !== null || this.options.childrenRunning()) return false;
        // Publish a complete ack in the private run directory, and enter normal teardown in the same JS turn.
        try {
          const temporary = join(run, `quit-ack-${randomUUID()}`);
          writeFileSync(temporary, JSON.stringify(token), { mode: 0o600, flag: "wx" });
          renameSync(temporary, join(run, "quit-ack.json"));
        } catch {
          return false;
        }
        this.options.quit();
        return true;
      });
    },
    Effect.catch(() => Effect.void),
  );
}
