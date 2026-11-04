// Disposable Electron fixture for renderer-startup-state-e2e.ts.
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { createServer, type ServerResponse } from "node:http";
import { join } from "node:path";
import { type CentralAuthState, IPC_ENDPOINTS } from "@openbot/contracts/ipc";
import { app, BrowserWindow, ipcMain } from "electron";
import { sendToRenderer, whenRendererCanReceive } from "../src/main/renderer-ipc";

const fixturePath = process.argv.find((arg) => arg.startsWith("--fixture-dir="))?.slice("--fixture-dir=".length);
assert.ok(fixturePath);
const root = fixturePath;
app.setPath("userData", join(root, "profile"));
const preload = join(root, "preload.cjs");
writeFileSync(
  preload,
  `
const { ipcRenderer } = require('electron');
const received = [];
ipcRenderer.on('auth:event', (_, state) => { received.push(state.status); ipcRenderer.send('fixture:received', received); });
ipcRenderer.on('servers:event', (_, servers) => ipcRenderer.send('fixture:servers', servers));
ipcRenderer.invoke('auth:get-state').then(state => ipcRenderer.send('fixture:initial', state.status));
`,
);
const report: { cases: string[]; passed: boolean; error?: string } = { cases: [], passed: false };
const deadline = setTimeout(() => finish(new Error("Fixture timed out")), 20_000);
let server: ReturnType<typeof createServer> | undefined;
function finish(error?: unknown) {
  clearTimeout(deadline);
  if (error) report.error = String(error);
  report.passed = !error;
  writeFileSync(join(root, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  server?.close();
  app.exit(error ? 1 : 0);
}

void app
  .whenReady()
  .then(async () => {
    let image: ServerResponse | undefined;
    let initialRead = false;
    const release = () => {
      if (initialRead && image) image.end();
    };
    server = createServer((request, response) => {
      if (request.url === "/hold") {
        image = response;
        release();
      } else {
        response.end('<!doctype html><img src="/hold">');
      }
    });
    await new Promise<void>((resolve) => server?.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    assert.ok(address && typeof address === "object");
    const window = new BrowserWindow({
      show: false,
      webPreferences: { preload, sandbox: true, contextIsolation: true },
    });
    let state: CentralAuthState = { status: "loading" };
    ipcMain.handle(IPC_ENDPOINTS.auth.getState.channel, () => state);
    const received = new Promise<void>((resolve, reject) => {
      ipcMain.once("fixture:received", (_event, statuses) => {
        try {
          assert.deepEqual(statuses, ["signed_in"]);
          resolve();
        } catch (error) {
          reject(error);
        }
      });
    });
    const serversReceived = new Promise<void>((resolve, reject) => {
      ipcMain.once("fixture:servers", (_event, servers) => {
        try {
          assert.deepEqual(servers, []);
          resolve();
        } catch (error) {
          reject(error);
        }
      });
    });
    ipcMain.once("fixture:initial", (_event, status) => {
      assert.equal(status, "loading");
      state = {
        status: "signed_in",
        user: { id: "fixture", email: "fixture@example.invalid", name: null, avatarUrl: null },
      };
      assert.equal(sendToRenderer(window, IPC_ENDPOINTS.auth.event, state), false);
      report.cases.push("initial loading read; account event dropped during load");
      initialRead = true;
      release();
    });
    let sentAtFinish = true;
    window.webContents.once("did-finish-load", () => {
      sentAtFinish = sendToRenderer(window, IPC_ENDPOINTS.servers.event, []);
    });
    await window.loadURL(`http://127.0.0.1:${address.port}`);
    assert.equal(sentAtFinish, false, "did-finish-load is still too early for a send");
    const stops = window.webContents.listenerCount("did-stop-loading");
    const destroyed = window.webContents.listenerCount("destroyed");
    await whenRendererCanReceive(window);
    assert.equal(sendToRenderer(window, IPC_ENDPOINTS.servers.event, []), true);
    assert.equal(sendToRenderer(window, IPC_ENDPOINTS.auth.event, state), true);
    await Promise.all([received, serversReceived]);
    assert.equal(window.webContents.listenerCount("did-stop-loading"), stops);
    assert.equal(window.webContents.listenerCount("destroyed"), destroyed);
    report.cases.push("latest account and server state delivered after loading stops without reload");
    await whenRendererCanReceive(window);
    report.cases.push("already loaded window resolves immediately");
    window.destroy();
    await whenRendererCanReceive(window);
    assert.equal(sendToRenderer(window, IPC_ENDPOINTS.auth.event, state), false);
    report.cases.push("destroyed window resolves and rejects sends");
    const closing = new BrowserWindow({ show: false });
    const loading = closing.loadURL(`http://127.0.0.1:${address.port}`).catch(() => undefined);
    const pending = whenRendererCanReceive(closing);
    closing.destroy();
    await pending;
    await loading;
    report.cases.push("window destroyed during loading settles the wait");
    finish();
  })
  .catch(finish);
