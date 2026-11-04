// Exercise the actual main-process click guard in an isolated Chromium world. Never start the updater.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir, userInfo } from "node:os";
import { join, resolve } from "node:path";
import { translateFor } from "@openbot/i18n";
import solid from "@solidjs/vite-plugin";
import { Effect } from "effect";
import { chromium, type Page } from "playwright-core";
import { createServer } from "vite";
import { ARM_FORK_UPDATE_CLICK, CONSUME_FORK_UPDATE_CLICK, forkUpdateReport } from "../src/main/fork-update";

import { readOwnedJson } from "../src/main/host-update-files";

async function checkRetryRow(page: Page) {
  const owner = await mkdtemp(join(tmpdir(), "fork-settings-"));
  const file = join(owner, "status.json");
  const record = (value: { phase: string; failedStep?: string; reason?: string }) =>
    writeFile(file, JSON.stringify(value), { mode: 0o600 });
  const t = translateFor("en");
  const server = await createServer({
    root: resolve(import.meta.dirname, "fork-update"),
    configFile: false,
    plugins: [solid()],
    resolve: { dedupe: ["solid-js", "@solidjs/web"] },
    optimizeDeps: { entries: ["settings-fixture.html"] },
    server: { host: "127.0.0.1", port: 0, watch: null },
    logLevel: "error",
  });
  try {
    await server.listen();
    const address = server.httpServer?.address();
    assert.ok(address && typeof address === "object");
    const session = await page.context().newCDPSession(page);
    async function inOwnerWorld(expression: string) {
      const { frameTree } = await session.send("Page.getFrameTree");
      const { executionContextId } = await session.send("Page.createIsolatedWorld", {
        frameId: frameTree.frame.id,
        worldName: "fork-update-settings",
      });
      return (await session.send("Runtime.evaluate", { contextId: executionContextId, expression })).result.value;
    }
    let starts = 0;
    await page.exposeFunction("fixtureReadPhase", async () => {
      await inOwnerWorld(ARM_FORK_UPDATE_CLICK);
      return (await Effect.runPromise(readOwnedJson(file, userInfo().uid, forkUpdateReport))).phase;
    });
    await page.exposeFunction("fixtureStart", async () => {
      if ((await inOwnerWorld(CONSUME_FORK_UPDATE_CLICK)) !== true) return null;
      starts++;
      await record({ phase: "checking" });
      return "checking";
    });
    await record({ phase: "failed", reason: "/private/example-path" });
    await page.goto(`http://127.0.0.1:${address.port}/settings-fixture.html`);
    const retry = page.getByRole("button", { name: t("common.retry"), exact: true });
    await retry.waitFor();
    await page.getByText(t("update.fork.failed"), { exact: true }).waitFor();
    await record({ phase: "failed", failedStep: "quitHelperTimeout", reason: "/private/example-path" });
    await page.reload();
    await page.getByText(t("update.fork.failed.quitHelperTimeout"), { exact: true }).waitFor();
    assert.equal(await page.getByText("/private/example-path").count(), 0);
    await page.getByRole("main").screenshot({ path: ".openbot-build/fork-update-click/failed-retry.png" });
    await retry.evaluate((button) => {
      if (button instanceof HTMLElement) button.click();
    });
    await retry.waitFor();
    assert.equal(starts, 0);
    await retry.click();
    await page.getByText(t("update.fork.checking"), { exact: true }).waitFor();
    assert.equal(starts, 1);
    assert.ok(await page.getByRole("button", { name: t("update.fork.start") }).isDisabled());
    await record({ phase: "complete" });
    await page.getByText(t("update.fork.complete"), { exact: true }).waitFor();
    assert.ok(await page.getByRole("button", { name: t("update.fork.start") }).isEnabled());
    return {
      legacyFailure: true,
      savedQuitFailure: true,
      privateDetailHidden: true,
      retryStarts: starts,
      completed: true,
    };
  } finally {
    await server.close();
    await rm(owner, { force: true, recursive: true });
  }
}

const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const page = await browser.newPage();
  await page.setContent(
    '<div data-tab="updates"><button id="openbot-owner-fork-update">Update this Mac</button><button>Other</button></div>',
  );
  const cdp = await page.context().newCDPSession(page);
  const { frameTree } = await cdp.send("Page.getFrameTree");
  const { executionContextId } = await cdp.send("Page.createIsolatedWorld", {
    frameId: frameTree.frame.id,
    worldName: "fork-update-click",
  });
  const evaluate = async (expression: string) =>
    (await cdp.send("Runtime.evaluate", { contextId: executionContextId, expression, returnByValue: true })).result
      .value;
  await evaluate(ARM_FORK_UPDATE_CLICK);
  assert.equal(await evaluate(CONSUME_FORK_UPDATE_CLICK), false);
  await page.evaluate("window.consumeOpenBotForkUpdateClick = () => true");
  assert.equal(await evaluate(CONSUME_FORK_UPDATE_CLICK), false);
  const settings = page.getByRole("button", { name: "Update this Mac" });
  await settings.evaluate((button) => {
    if (button instanceof HTMLElement) button.click();
  });
  assert.equal(await evaluate(CONSUME_FORK_UPDATE_CLICK), false);
  await page.getByRole("button", { name: "Other" }).click();
  assert.equal(await evaluate(CONSUME_FORK_UPDATE_CLICK), false);
  await settings.click();
  assert.equal(await evaluate(CONSUME_FORK_UPDATE_CLICK), true);
  assert.equal(await evaluate(CONSUME_FORK_UPDATE_CLICK), false);
  await settings.focus();
  await page.keyboard.press("Enter");
  assert.equal(await evaluate(CONSUME_FORK_UPDATE_CLICK), true);
  await mkdir(".openbot-build/fork-update-click", { recursive: true });
  await writeFile(
    ".openbot-build/fork-update-click/report.json",
    JSON.stringify(
      {
        unrelatedClick: "refused",
        directCall: "refused",
        pageOverride: "refused",
        syntheticClick: "refused",
        realClick: "accepted",
        replay: "refused",
        keyboard: "accepted",
        settings: await checkRetryRow(page),
      },
      null,
      2,
    ),
  );
} finally {
  await browser.close();
}
