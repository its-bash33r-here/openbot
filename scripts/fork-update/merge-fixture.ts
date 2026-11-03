// Run the production response protocol with a deterministic provider and owner window for the Git E2E.
import { appendFileSync, readFileSync } from "node:fs";
import { userInfo } from "node:os";
import { join } from "node:path";
import { Effect } from "effect";
import { forkMergeResponder } from "../../src/main/fork-merge";

const [run, nonce, once] = process.argv.slice(2);
if (!run || !nonce) throw new Error("The fixture needs its run directory and nonce.");
const mode = () => readFileSync(join(run, "fixture-mode"), "utf8");
const record = (event: string) => appendFileSync(join(run, "fixture-events"), `${event}\n`);
const respond = forkMergeResponder({
  run,
  nonce,
  uid: userInfo().uid,
  active: () => true,
  generate: (prompt) =>
    Effect.sync(() => {
      const value = JSON.parse(prompt.slice(prompt.lastIndexOf("\n") + 1));
      if (value.path !== "guide.md") throw new Error("Unexpected provider input");
      record("model");
      if (mode() === "words") return JSON.stringify({ action: "merged", text: "Show two bright stars.\n" });
      if (mode() === "invent") return JSON.stringify({ action: "merged", text: "unexpected code\n" });
      if (mode() === "ask") return JSON.stringify({ action: "ask" });
      return JSON.stringify({ action: "merged", text: value.current + value.incoming });
    }),
  choose: async (request) => {
    record(`window ${request.path}`);
    return mode() === "choose" ? "incoming" : "later";
  },
});
if (once) await Effect.runPromise(respond());
else setInterval(() => Effect.runFork(respond()), 40);
