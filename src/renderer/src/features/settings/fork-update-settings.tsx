import { type ForkUpdatePhase, forkUpdateInProgress, isForkUpdateRetryPhase } from "@openbot/contracts/ipc";
import type { AppTextKey } from "@openbot/i18n";
import { Button, Item, ItemActions, ItemContent, ItemDescription, ItemTitle } from "@openbot/ui";
import { useText } from "@openbot/ui/text";
import { createEffect, createSignal, onCleanup } from "solid-js";
import { settingsPort } from "./settings-port";

const PHASE_TEXT = {
  idle: "update.fork.idle",
  syncing: "update.fork.syncing",
  checking: "update.fork.checking",
  signing: "update.fork.signing",
  publishing: "update.fork.publishing",
  installing: "update.fork.installing",
  upToDate: "update.upToDate",
  complete: "update.fork.complete",
  conflict: "update.fork.conflict",
  held: "update.fork.held",
  failed: "update.fork.failed",
  "failed.sync": "update.fork.failed.sync",
  "failed.checks": "update.fork.failed.checks",
  "failed.sign": "update.fork.failed.sign",
  "failed.quit": "update.fork.failed.quit",
  "failed.quitHelperTimeout": "update.fork.failed.quitHelperTimeout",
  "failed.backup": "update.fork.failed.backup",
  "failed.verifyCopy": "update.fork.failed.verifyCopy",
  "failed.publish": "update.fork.failed.publish",
  "failed.install": "update.fork.failed.install",
  "failed.launch": "update.fork.failed.launch",
} as const satisfies Record<ForkUpdatePhase, AppTextKey>;

export function createForkUpdateSettings(visible: () => boolean) {
  const { t } = useText();
  const [phase, setPhase] = createSignal<ForkUpdatePhase | null>(null);
  let generation = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  function receive(next: ForkUpdatePhase | null, request: number): void {
    if (request !== generation) return;
    setPhase(next);
    if (next && forkUpdateInProgress(next)) timer = setTimeout(() => read(request), 2000);
  }

  function read(request: number): void {
    void settingsPort()
      .update.getForkPhase()
      .then(
        (next) => receive(next, request),
        () => receive(null, request),
      );
  }

  createEffect(visible, (shown) => {
    clearTimeout(timer);
    generation++;
    setPhase(null);
    if (shown) read(generation);
  });
  onCleanup(() => {
    generation++;
    clearTimeout(timer);
  });

  function start(): void {
    const request = ++generation;
    clearTimeout(timer);
    setPhase("syncing");
    void settingsPort()
      .update.startFork()
      .then(
        (next) => {
          if (next === null && request === generation) read(request);
          else receive(next, request);
        },
        () => receive("failed", request),
      );
  }

  return {
    available: () => phase() !== null,
    Row: () => (
      <Item class="settings-modal-row settings-modal-update-row">
        <ItemContent>
          <ItemTitle>{t("update.fork.title")}</ItemTitle>
          <ItemDescription aria-live="polite">{t(PHASE_TEXT[phase() ?? "idle"])}</ItemDescription>
        </ItemContent>
        <ItemActions>
          <Button
            id="openbot-owner-fork-update"
            variant="outline"
            size="sm"
            type="button"
            disabled={forkUpdateInProgress(phase() ?? "idle")}
            onClick={start}
          >
            {t(isForkUpdateRetryPhase(phase()) ? "common.retry" : "update.fork.start")}
          </Button>
        </ItemActions>
      </Item>
    ),
  };
}
