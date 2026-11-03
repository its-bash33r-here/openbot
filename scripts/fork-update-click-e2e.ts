// Exercise the actual main-process click guard in an isolated Chromium world. Never start the updater.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { chromium } from "playwright-core";
import { ARM_FORK_UPDATE_CLICK, CONSUME_FORK_UPDATE_CLICK } from "../src/main/fork-update";

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
      },
      null,
      2,
    ),
  );
} finally {
  await browser.close();
}
