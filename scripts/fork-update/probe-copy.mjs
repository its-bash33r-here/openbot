// Attach only to the disposable app that owns this port. Keep chat text and credentials in the app.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const [pid, profile, expected] = process.argv.slice(2);
const probe = `(async () => {
  const app = window.openbot;
  const state = await app.auth.getState();
  if (state.status !== ${JSON.stringify(expected)}) return "session";
  if (performance.getEntriesByType("navigation")[0]?.type !== "navigate") return "navigation";
  const workspaceVisible = document.querySelector(".app-window")?.checkVisibility() === true
    && !document.querySelector("main.app-loading")?.checkVisibility();
  if (state.status === "signed_in" && (await app.getSetupState()).completed && !workspaceVisible) return "workspace";
  const agents = await app.agent.listAgents();
  if (agents.length) await app.agent.readConversationPage({
    agentId: agents[0].id, anchor: {type: "latest"}, limit: 1
  });
  return { status: state.status, workspaceVisible, navigation: "navigate" };
})()`;

async function send(url, method, params) {
  const ws = new WebSocket(url);
  try {
    return await new Promise((resolve, reject) => {
      ws.onopen = () => ws.send(JSON.stringify({ id: 1, method, params }));
      ws.onerror = () => reject(new Error("The QA debugger is unavailable."));
      ws.onmessage = (e) => {
        const reply = JSON.parse(e.data);
        if (reply.id === 1) resolve(reply.result);
      };
      ws.onclose = () => (method === "Browser.close" ? resolve(null) : reject(new Error("QA app closed.")));
    });
  } finally {
    ws.close();
  }
}

const started = Date.now();
const deadline = started + 85_000;
let passed = false;
let waitingFor = "window";
while (!passed && Date.now() < deadline) {
  try {
    process.kill(Number(pid), 0);
  } catch {
    break;
  }
  try {
    const port = Number(readFileSync(join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]);
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("Invalid QA port.");
    const owner = execFileSync("/usr/sbin/lsof", ["-nP", "-t", `-iTCP:${port}`, "-sTCP:LISTEN"], {
      stdio: ["ignore", "pipe", "ignore"],
    })
      .toString()
      .trim();
    if (owner !== pid) throw new Error("QA port belongs to another process.");
    const base = `http://127.0.0.1:${port}`;
    const pages = await (await fetch(`${base}/json/list`)).json();
    const page = pages.find((target) => target.type === "page" && target.url === "openbot-app://app/index.html");
    if (page) {
      const reply = await send(page.webSocketDebuggerUrl, "Runtime.evaluate", {
        expression: probe,
        awaitPromise: true,
        returnByValue: true,
      });
      if (reply?.exceptionDetails) waitingFor = "probe";
      const result = reply?.result?.value;
      if (["session", "navigation", "workspace"].includes(result)) waitingFor = result;
      if (result?.status === expected && result.navigation === "navigate") {
        const version = await (await fetch(`${base}/json/version`)).json();
        await send(version.webSocketDebuggerUrl, "Browser.close", {});
        process.stdout.write(
          JSON.stringify({
            workspaceVisible: result.workspaceVisible,
            navigation: result.navigation,
            elapsedMs: Date.now() - started,
          }),
        );
        passed = true;
      }
    }
  } catch {
    // The port file and app services become ready independently.
  }
  if (!passed) await delay(250);
}
if (!passed) process.stdout.write(JSON.stringify({ waitingFor }));
process.exitCode = passed ? 0 : 1;
